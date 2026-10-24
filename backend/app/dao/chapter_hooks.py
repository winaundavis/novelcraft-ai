"""chapter_hooks 表 DAO（Data Access Object）—— M3 钩子账本（短程跨章张力）。

钩子（Hook）是章节结尾埋下的强钩子，驱动读者继续读下一章；状态机：
planted（已埋设）→ carried（已承接未解决）→ resolved（已回收）。

与长程伏笔（P2，foreshadows 表）职责分离：钩子管"短程跨章张力"，
伏笔管"跨卷承诺/待兑现"。

设计：project_id + chapter_id 唯一（每章至多一个钩子），UPSERT 幂等。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_CHAPTER_HOOKS
from app.db import get_conn
from app.models.schemas import gen_id

HOOK_STATUS_PLANTED = "planted"
HOOK_STATUS_CARRIED = "carried"
HOOK_STATUS_RESOLVED = "resolved"
VALID_HOOK_STATUSES = {HOOK_STATUS_PLANTED, HOOK_STATUS_CARRIED, HOOK_STATUS_RESOLVED}

# 6 种钩子类型（PRD M3-②）
HOOK_TYPE_SUSPENSE = "suspense"          # 悬念
HOOK_TYPE_TWIST = "twist"                # 反转
HOOK_TYPE_NEW_INFO = "new_info"          # 新信息
HOOK_TYPE_CRISIS = "crisis"              # 危机升级
HOOK_TYPE_EMOTIONAL = "emotional"        # 情感悬置
HOOK_TYPE_SCENE_SWITCH = "scene_switch"  # 场景切换预告
VALID_HOOK_TYPES = {
    HOOK_TYPE_SUSPENSE,
    HOOK_TYPE_TWIST,
    HOOK_TYPE_NEW_INFO,
    HOOK_TYPE_CRISIS,
    HOOK_TYPE_EMOTIONAL,
    HOOK_TYPE_SCENE_SWITCH,
}


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def upsert_hook(
    project_id: str,
    chapter_id: str,
    *,
    hook_type: str = HOOK_TYPE_SUSPENSE,
    status: str = HOOK_STATUS_PLANTED,
    target_chapter: str = "",
    description: str = "",
) -> None:
    """幂等写入某章的钩子记录（project_id + chapter_id 唯一，重复时更新）。"""
    if hook_type not in VALID_HOOK_TYPES:
        hook_type = HOOK_TYPE_SUSPENSE
    if status not in VALID_HOOK_STATUSES:
        status = HOOK_STATUS_PLANTED
    now = _utc_iso()
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TABLE_CHAPTER_HOOKS}
                (id, project_id, chapter_id, hook_type, status, target_chapter,
                 description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, chapter_id) DO UPDATE SET
                hook_type = excluded.hook_type,
                status = excluded.status,
                target_chapter = excluded.target_chapter,
                description = excluded.description,
                updated_at = excluded.updated_at
            """,
            (
                gen_id(),
                project_id,
                chapter_id or "",
                hook_type,
                status,
                target_chapter or "",
                (description or "").strip()[:500],
                now,
                now,
            ),
        )


def get_hook(project_id: str, chapter_id: str) -> dict[str, Any] | None:
    """按章节查询钩子记录（每章至多一条）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM {TABLE_CHAPTER_HOOKS}
            WHERE project_id = ? AND chapter_id = ?
            """,
            (project_id, chapter_id),
        ).fetchone()
    return dict(row) if row else None


def list_hooks(project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """按写入顺序返回项目全部钩子记录。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_CHAPTER_HOOKS}
            WHERE project_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def list_open_hooks(project_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """返回仍处于开放状态的钩子（planted/carried），供生成前注入\"先承接再埋新钩\"。

    按时间倒序（最近的先返回），成本控制：只取最近 limit 条。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_CHAPTER_HOOKS}
            WHERE project_id = ? AND status IN (?, ?)
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (project_id, HOOK_STATUS_PLANTED, HOOK_STATUS_CARRIED, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def update_hook_status(project_id: str, chapter_id: str, status: str) -> bool:
    """更新某章钩子的状态（planted → carried / resolved），返回是否更新成功。"""
    if status not in VALID_HOOK_STATUSES:
        return False
    with get_conn() as conn:
        cursor = conn.execute(
            f"""
            UPDATE {TABLE_CHAPTER_HOOKS}
            SET status = ?, updated_at = ?
            WHERE project_id = ? AND chapter_id = ?
            """,
            (status, _utc_iso(), project_id, chapter_id),
        )
        return cursor.rowcount > 0


def count_hooks(project_id: str) -> int:
    """统计某项目的钩子记录数（幂等性验证用）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {TABLE_CHAPTER_HOOKS} WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def delete_hooks_by_project(project_id: str) -> None:
    """删除某项目下的全部钩子记录（级联删除用）。"""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TABLE_CHAPTER_HOOKS} WHERE project_id = ?", (project_id,))
