"""volumes 表 DAO（Data Access Object）—— M1 卷级摘要。

卷是长篇小说的一组连续章节；已完结卷懒压缩为 300-600 字卷摘要，
供后续章节写作时快速回忆前情（50 万字长篇不丢前文的关键）。
"""

from __future__ import annotations

from typing import Any

from app.dao.constants import TABLE_VOLUMES
from app.db import get_conn, utc_today


def upsert_volume(
    project_id: str,
    volume_id: str,
    *,
    title: str = "",
    volume_order: int = 0,
    start_chapter_id: str = "",
    end_chapter_id: str = "",
    summary: str = "",
) -> None:
    """按卷 ID 插入或更新卷记录（幂等，重复调用不报错）。

    summary 合并规则：新值为空时不覆盖已有摘要（保证已生成的卷摘要不被清空）。
    """
    today = utc_today()
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TABLE_VOLUMES}
                (id, project_id, start_chapter_id, end_chapter_id, title, summary, "order", created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, id) DO UPDATE SET
                start_chapter_id = excluded.start_chapter_id,
                end_chapter_id = excluded.end_chapter_id,
                title = excluded.title,
                summary = COALESCE(NULLIF(excluded.summary, ''), {TABLE_VOLUMES}.summary),
                "order" = excluded."order",
                updated_at = excluded.updated_at
            """,
            (
                volume_id,
                project_id,
                start_chapter_id,
                end_chapter_id,
                title,
                summary,
                volume_order,
                today,
                today,
            ),
        )


def get_volume(project_id: str, volume_id: str) -> dict[str, Any] | None:
    """按卷 ID 查询单卷。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_VOLUMES} WHERE project_id = ? AND id = ?",
            (project_id, volume_id),
        ).fetchone()
    return dict(row) if row else None


def list_volumes(project_id: str) -> list[dict[str, Any]]:
    """返回某项目全部卷，按卷顺序排列。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_VOLUMES}
            WHERE project_id = ?
            ORDER BY "order" ASC, created_at ASC
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_volume_summaries(project_id: str) -> list[str]:
    """返回已生成摘要的卷摘要列表（按卷顺序，用于 prompt 注入）。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT summary FROM {TABLE_VOLUMES}
            WHERE project_id = ? AND summary != ''
            ORDER BY "order" ASC, created_at ASC
            """,
            (project_id,),
        ).fetchall()
    return [row["summary"] for row in rows]


def update_volume_summary(
    project_id: str, volume_id: str, summary: str, summary_hash: str = ""
) -> None:
    """更新某卷的摘要。

    记忆升级 v1：`summary_hash`（压缩时的"输入签名"＝卷内章摘要拼接 MD5）在列存在时
    一并写入，作为摘要对账（reconcile_volume_summaries）的漂移基线；老库无该列时
    自动降级（只更新 summary），绝不报错。
    """
    with get_conn() as conn:
        has_hash = any(
            row["name"] == "summary_hash"
            for row in conn.execute(f"PRAGMA table_info({TABLE_VOLUMES})").fetchall()
        )
        if has_hash:
            conn.execute(
                f"""
                UPDATE {TABLE_VOLUMES}
                SET summary = ?, summary_hash = ?, updated_at = ?
                WHERE project_id = ? AND id = ?
                """,
                (summary, summary_hash or "", utc_today(), project_id, volume_id),
            )
        else:
            conn.execute(
                f"""
                UPDATE {TABLE_VOLUMES}
                SET summary = ?, updated_at = ?
                WHERE project_id = ? AND id = ?
                """,
                (summary, utc_today(), project_id, volume_id),
            )


def delete_volumes_by_project(project_id: str) -> None:
    """删除某项目下的全部卷（级联删除用，SQLite 未启用外键约束时手动清理）。"""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TABLE_VOLUMES} WHERE project_id = ?", (project_id,))
