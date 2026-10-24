"""projects 表 DAO（Data Access Object）—— 封装所有对 projects 表的 SQL 操作。"""

from __future__ import annotations

import sqlite3
import json
from typing import Any

from app.dao.constants import TABLE_PROJECTS
from app.db import get_conn, json_dump, row_to_project


def list_projects(owner_id: str | None = None) -> list[dict[str, Any]]:
    """获取全部项目，按收藏优先 + 更新时间倒序排列。"""
    with get_conn() as conn:
        if owner_id is None:
            rows = conn.execute(f"SELECT * FROM {TABLE_PROJECTS} ORDER BY is_starred DESC, updated_at DESC, created_at DESC").fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM {TABLE_PROJECTS} WHERE owner_id = ? ORDER BY is_starred DESC, updated_at DESC, created_at DESC", (owner_id,)).fetchall()
    return [row_to_project(row) for row in rows]


def get_project(project_id: str) -> dict[str, Any] | None:
    """按主键查询单个项目。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_PROJECTS} WHERE id = ?", (project_id,)
        ).fetchone()
    return row_to_project(row) if row else None


def project_exists(project_id: str) -> bool:
    """快速检查项目是否存在。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT 1 FROM {TABLE_PROJECTS} WHERE id = ?", (project_id,)
        ).fetchone()
    return row is not None


def get_project_owner(project_id: str) -> str | None:
    with get_conn() as conn:
        try:
            row = conn.execute(f"SELECT owner_id FROM {TABLE_PROJECTS} WHERE id = ?", (project_id,)).fetchone()
            return str(row["owner_id"]) if row else None
        except sqlite3.OperationalError as exc:
            # A process normally runs init_db() before serving requests. This fallback
            # keeps pre-migration SQLite data readable until that migration runs.
            if "owner_id" not in str(exc):
                raise
            row = conn.execute(f"SELECT 1 FROM {TABLE_PROJECTS} WHERE id = ?", (project_id,)).fetchone()
            return "local-dev" if row else None


def create_project(fields: dict[str, Any]) -> None:
    """插入一条项目记录。
    
    fields 字典的键为数据库列名（snake_case），值为已序列化的数据。
    """
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = tuple(fields[col] for col in columns)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {TABLE_PROJECTS} ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


def update_project_fields(project_id: str, field_updates: dict[str, Any], updated_at: str) -> int:
    """按列名更新项目字段，返回受影响行数（0 表示项目不存在）。
    
    field_updates: {column_name: value} — 键为数据库列名（snake_case），值已序列化。
    updated_at: 更新时的时间戳字符串。
    """
    if not field_updates:
        return 0
    set_clauses = [f"{col} = ?" for col in field_updates]
    params = list(field_updates.values())
    set_clauses.append("updated_at = ?")
    params.append(updated_at)
    params.append(project_id)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if "outline" in field_updates:
            from app.services.chapter_trash_service import reconcile_outline_on_conn
            row = conn.execute("SELECT outline FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row:
                reconcile_outline_on_conn(conn, project_id,
                    json.loads(row["outline"] or "null") or {},
                    json.loads(field_updates["outline"] or "null") or {})
        result = conn.execute(
            f"UPDATE {TABLE_PROJECTS} SET {', '.join(set_clauses)} WHERE id = ?",
            tuple(params),
        )
        return result.rowcount


def update_project_word_count(project_id: str, today: str) -> None:
    """重算并更新项目的 current_word_count（从 chapters 表聚合）。"""
    with get_conn() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_PROJECTS}
            SET current_word_count = (
                SELECT COALESCE(SUM(word_count), 0) FROM chapters WHERE project_id = ?
            ),
            updated_at = ?
            WHERE id = ?
            """,
            (project_id, today, project_id),
        )


def update_project_word_count_and_status(project_id: str, total_words: int, status: str, today: str) -> None:
    """直接设置项目的 current_word_count、status、updated_at。"""
    with get_conn() as conn:
        conn.execute(
            f"""
            UPDATE {TABLE_PROJECTS}
            SET current_word_count = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (total_words, status, today, project_id),
        )


def delete_project(project_id: str) -> int:
    """Atomically delete project-owned data; preserve personal reference archives."""
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return delete_project_on_conn(conn, project_id)


def delete_project_on_conn(conn: sqlite3.Connection, project_id: str) -> int:
    """Caller owns commit/rollback; every deletion shares this connection."""
    if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        return 0
    available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "chat_conversations" in available:
        from app.dao import chat_conversations

        chat_conversations.delete_project_on_conn(conn, project_id)
    # Explicit ownership: reference_works belongs to the author and deliberately
    # retains its source project ID after deletion. Only its project binding goes.
    for table in (
        "chapter_trash", "novel_jobs", "chapter_commit_history", "chapter_commits",
        "story_state_transitions", "chapter_digests", "memory_recall_log",
        "memory_projections", "chapter_contracts", "story_states",
        "chapter_rewrites", "outline_candidates", "chapter_hooks", "foreshadows",
        "character_voice_profiles", "writing_experiences", "memory_relations",
        "memory_entities", "chapter_fts", "novel_facts", "reference_bindings",
        "style_transitions", "style_bindings", "style_profiles", "volumes", "chapters",
    ):
        # Older/minimal schemas may omit optional subsystems (including FTS5).
        if table in available:
            conn.execute(f'DELETE FROM "{table}" WHERE project_id = ?', (project_id,))
    return conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount


# ── 事务内辅助函数（接受外部 conn，用于多步操作在同一事务中执行） ──

def _update_project_word_count_on_conn(conn, project_id: str, today: str) -> None:
    """在给定连接上重算并更新项目的 current_word_count（供外部事务块使用）。"""
    conn.execute(
        f"""
        UPDATE {TABLE_PROJECTS}
        SET current_word_count = (
            SELECT COALESCE(SUM(word_count), 0) FROM chapters WHERE project_id = ?
        ),
        updated_at = ?
        WHERE id = ?
        """,
        (project_id, today, project_id),
    )


def _update_project_word_count_and_status_on_conn(conn, project_id: str, total_words: int, status: str, today: str) -> None:
    """在给定连接上直接设置项目的 current_word_count、status、updated_at（供外部事务块使用）。"""
    conn.execute(
        f"""
        UPDATE {TABLE_PROJECTS}
        SET current_word_count = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (total_words, status, today, project_id),
    )
