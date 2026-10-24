"""阶段E：章节提交记录 + 记忆投影状态（借鉴 Webnovel Writer「提交链/投影日志」思想，
自研实现，非复制 GPL 代码）。

- chapter_commits：一章写完，正文哈希 + 上下文哈希 + audit_id + 模型来源 + 完成时间
- memory_projections：章节摘要 / 事实 / 实体 / 卷摘要 / FTS 五路投影各自 done/failed/skipped，
  某项失败只重跑该投影
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db import get_conn
from app.models.schemas import gen_id


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def sha256_hex(text: str) -> str:
    """正文/上下文哈希（SHA-256 十六进制前缀，可追溯不落原文）。"""
    if not text:
        return ""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


# ── 章节提交记录（不可变版本链，P1 Codex 复测）──

HISTORY_TABLE = "chapter_commit_history"
SNAPSHOT_TABLE = "chapter_commits"


def record_commit_on_conn(
    conn: Any,
    *,
    project_id: str,
    chapter_id: str,
    content_hash: str,
    context_hash: str,
    audit_id: str = "",
    provider: str = "",
    model: str = "",
    contract_version: int = 0,
    style_profile_version: int = 0,
    story_state_version_before: int = 0,
    content_snapshot: str = "",
    observed_facts: list[Any] | None = None,
    state_changes: list[Any] | None = None,
    relationship_changes: list[Any] | None = None,
    knowledge_changes: list[Any] | None = None,
    conflict_changes: list[Any] | None = None,
    foreshadow_changes: list[Any] | None = None,
    payoffs: list[Any] | None = None,
    unresolved_questions: list[Any] | None = None,
    next_direction: str = "",
    created_by: str = "system",
    status: str = "accepted",
) -> str:
    """在同一事务 conn 内追加一条不可变 commit（version 递增 + parent 链接）。

    与正文落库共用同一连接/事务：正文保存与 commit 记录原子一致——
    任一失败整体回滚，避免"正文已变但审计缺失"（P1 Codex 复测）。
    返回新 commit id。
    """
    now = _utc_iso()
    last = conn.execute(
        f"SELECT id, version FROM {HISTORY_TABLE} "
        "WHERE project_id = ? AND chapter_id = ? ORDER BY version DESC LIMIT 1",
        (project_id, chapter_id),
    ).fetchone()
    version = int(last["version"] or 0) + 1 if last else 1
    parent_id = (last["id"] or "") if last else ""
    # A chapter has one active canonical commit.  Previous accepted versions
    # remain immutable history but are explicitly superseded.
    if last and str(status or "accepted") == "accepted":
        conn.execute(
            f"UPDATE {HISTORY_TABLE} SET status = 'superseded' "
            "WHERE project_id = ? AND chapter_id = ? AND status = 'accepted'",
            (project_id, chapter_id),
        )
    commit_id = gen_id()
    available = {row["name"] for row in conn.execute(f"PRAGMA table_info({HISTORY_TABLE})").fetchall()}
    fields: dict[str, Any] = {
        "id": commit_id, "project_id": project_id, "chapter_id": chapter_id, "version": version,
        "parent_commit_id": parent_id, "content_hash": content_hash, "context_hash": context_hash,
        "audit_id": audit_id, "provider": provider, "model": model, "created_at": now,
        "contract_version": max(0, int(contract_version or 0)),
        "style_profile_version": max(0, int(style_profile_version or 0)),
        "story_state_version_before": max(0, int(story_state_version_before or 0)),
        "content_snapshot": content_snapshot,
        "observed_facts_json": json.dumps(observed_facts or [], ensure_ascii=False),
        "state_changes_json": json.dumps(state_changes or [], ensure_ascii=False),
        "relationship_changes_json": json.dumps(relationship_changes or [], ensure_ascii=False),
        "knowledge_changes_json": json.dumps(knowledge_changes or [], ensure_ascii=False),
        "conflict_changes_json": json.dumps(conflict_changes or [], ensure_ascii=False),
        "foreshadow_changes_json": json.dumps(foreshadow_changes or [], ensure_ascii=False),
        "payoffs_json": json.dumps(payoffs or [], ensure_ascii=False),
        "unresolved_questions_json": json.dumps(unresolved_questions or [], ensure_ascii=False),
        "next_direction": str(next_direction or "")[:600], "created_by": str(created_by or "system")[:80],
        "status": str(status or "accepted")[:40],
    }
    columns = [name for name in fields if name in available]
    conn.execute(
        f"INSERT INTO {HISTORY_TABLE} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [fields[name] for name in columns],
    )
    return commit_id


def record_commit(
    *,
    project_id: str,
    chapter_id: str,
    content_hash: str,
    context_hash: str,
    audit_id: str = "",
    provider: str = "",
    model: str = "",
    **evidence: Any,
) -> str:
    """追加一条不可变 commit（独立事务，供非正文落库场景使用）。"""
    with get_conn() as conn:
        return record_commit_on_conn(
            conn,
            project_id=project_id,
            chapter_id=chapter_id,
            content_hash=content_hash,
            context_hash=context_hash,
            audit_id=audit_id,
            provider=provider,
            model=model,
            **evidence,
        )


def get_commit(project_id: str, chapter_id: str) -> dict[str, Any] | None:
    """返回该章最新一条 commit（快照视图，向后兼容）。"""
    return get_canonical_commit(project_id, chapter_id)


def get_canonical_commit(project_id: str, chapter_id: str) -> dict[str, Any] | None:
    """Return the single active accepted commit for a chapter."""
    with get_conn() as conn:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({HISTORY_TABLE})").fetchall()}
        status_clause = "AND COALESCE(status, 'accepted') = 'accepted'" if "status" in columns else ""
        row = conn.execute(
            f"SELECT * FROM {HISTORY_TABLE} WHERE project_id = ? AND chapter_id = ? "
            f"{status_clause} "
            "ORDER BY version DESC LIMIT 1",
            (project_id, chapter_id),
        ).fetchone()
    return _decode_commit(row)


def list_canonical_commits(project_id: str) -> list[dict[str, Any]]:
    """Return one canonical commit per chapter in narrative order."""
    with get_conn() as conn:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({HISTORY_TABLE})").fetchall()}
        status_clause = "AND COALESCE(h.status, 'accepted') = 'accepted'" if "status" in columns else ""
        inner_status = "AND COALESCE(h2.status, 'accepted') = 'accepted'" if "status" in columns else ""
        rows = conn.execute(
            f"""SELECT h.* FROM {HISTORY_TABLE} h
                JOIN chapters c ON c.id = h.chapter_id AND c.project_id = h.project_id
                WHERE h.project_id = ? {status_clause}
                AND h.version = (SELECT MAX(h2.version) FROM {HISTORY_TABLE} h2
                                  WHERE h2.project_id = h.project_id AND h2.chapter_id = h.chapter_id
                                  {inner_status})
                ORDER BY c.chapter_order, c.created_at, h.version""",
            (project_id,),
        ).fetchall()
    return [_decode_commit(row) for row in rows]


def get_commit_history(project_id: str, chapter_id: str) -> list[dict[str, Any]]:
    """返回该章完整 commit 版本链（version 升序，含 parent_commit_id）。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM {HISTORY_TABLE} WHERE project_id = ? AND chapter_id = ? "
            "ORDER BY version ASC",
            (project_id, chapter_id),
        ).fetchall()
    return [_decode_commit(r) for r in rows]


def get_commit_by_id(commit_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(f"SELECT * FROM {HISTORY_TABLE} WHERE id = ?", (commit_id,)).fetchone()
    return _decode_commit(row)


def _decode_commit(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in (
        "observed_facts", "state_changes", "relationship_changes", "knowledge_changes",
        "conflict_changes", "foreshadow_changes", "payoffs", "unresolved_questions",
    ):
        try:
            value = json.loads(result.get(f"{key}_json") or "[]")
        except (TypeError, ValueError):
            value = []
        result[key] = value if isinstance(value, list) else []
    return result


def content_matches_commit(project_id: str, chapter_id: str) -> bool | None:
    commit = get_commit(project_id, chapter_id)
    if commit is None:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT content FROM chapters WHERE project_id = ? AND id = ?", (project_id, chapter_id)).fetchone()
    return bool(row and sha256_hex(row["content"] or "") == commit.get("content_hash"))


# ── 记忆投影状态 ─────────────────────────────────────

PROJECTION_TYPES = ("chapter_summary", "facts", "entities", "volume", "fts")


def set_projection(
    *,
    project_id: str,
    chapter_id: str,
    projection_type: str,
    status: str,  # done / failed / skipped
    detail: str = "",
    commit_id: str = "",
) -> None:
    """记录某路投影状态（幂等 upsert）。"""
    now = _utc_iso()
    with get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_projections)").fetchall()}
        if "commit_id" not in columns:
            conn.execute(
                """
                INSERT INTO memory_projections
                    (id, project_id, chapter_id, projection_type, status, detail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, chapter_id, projection_type) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (gen_id(), project_id, chapter_id, projection_type, status, detail, now, now),
            )
            return
        conn.execute(
            """
            INSERT INTO memory_projections
                (id, project_id, chapter_id, projection_type, status, detail, commit_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, chapter_id, projection_type) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                commit_id = excluded.commit_id,
                updated_at = excluded.updated_at
            """,
            (
                gen_id(), project_id, chapter_id, projection_type, status, detail, commit_id, now, now,
            ),
        )


def list_projections(project_id: str, chapter_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_projections WHERE project_id = ? AND chapter_id = ? "
            "ORDER BY projection_type ASC",
            (project_id, chapter_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_failed_projections(project_id: str, chapter_id: str = "") -> list[dict[str, Any]]:
    """返回 failed 状态的投影（用于单项重放）。"""
    with get_conn() as conn:
        if chapter_id:
            rows = conn.execute(
                "SELECT * FROM memory_projections WHERE project_id = ? AND chapter_id = ? "
                "AND status = 'failed' ORDER BY projection_type ASC",
                (project_id, chapter_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_projections WHERE project_id = ? AND status = 'failed' "
                "ORDER BY updated_at DESC LIMIT 200",
                (project_id,),
            ).fetchall()
    return [dict(r) for r in rows]
