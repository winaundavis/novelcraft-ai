"""chapter_rewrites 表 DAO —— 文笔局部重写审计痕迹（Codex 方案执行顺序第 8 步）。

每次局部重写（开头/对话/行动/情绪/结尾）都落一条审计记录：
- original      原文（重写前）
- rewritten     重写结果（重写后）
- reason        重写原因（触发维度 + 评分）
- score_before / score_after  综合评分前后（量表加权 10 分制）
- rewrite_type  开头 opening / 对话 dialogue / 行动 action / 情绪 emotion / 结尾 ending
- status        状态机：proposed（待人工确认，默认）/ applied（已应用）/
                 reverted（自动回退——重写后事实一致性或综合评分下降）

设计原则（数据安全）：
- 重写服务本身**不直接改写 chapters.content**，只把 original/rewritten 写入本表
  供人工确认；`rewrite_service.apply_rewrite` 提供显式「应用重写」入口。
- 建表由 db.init_db 用 CREATE TABLE IF NOT EXISTS 幂等创建，重复初始化无副作用，
  现有 26 项目数据不受影响。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_CHAPTER_REWRITES
from app.db import get_conn
from app.models.schemas import gen_id

# ── 状态机 ─────────────────────────────────────────────
REWRITE_STATUS_PROPOSED = "proposed"   # 已生成重写稿，待人工确认（默认）
REWRITE_STATUS_APPLIED = "applied"     # 已应用（自动校验通过后应用，或人工确认后应用）
REWRITE_STATUS_REVERTED = "reverted"   # 自动回退（事实一致性 FAIL 或综合评分下降）
REWRITE_STATUS_REJECTED = "rejected"
VALID_REWRITE_STATUSES = {
    REWRITE_STATUS_PROPOSED,
    REWRITE_STATUS_APPLIED,
    REWRITE_STATUS_REVERTED,
    REWRITE_STATUS_REJECTED,
}


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def insert_rewrite(
    project_id: str,
    chapter_id: str,
    rewrite_type: str,
    *,
    original: str,
    rewritten: str,
    reason: str = "",
    score_before: float = 0.0,
    score_after: float = 0.0,
    status: str = REWRITE_STATUS_PROPOSED,
) -> str:
    """写入一条局部重写审计记录，返回记录 id。

    - original/rewritten 均完整落库（审计痕迹，可回溯 diff 与人工比对）；
    - status 非法时归一为 proposed；
    - 幂等：每次调用生成新 id，不做覆盖（审计不可变，只更新状态列）。
    """
    if status not in VALID_REWRITE_STATUSES:
        status = REWRITE_STATUS_PROPOSED
    now = _utc_iso()
    record_id = gen_id()
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TABLE_CHAPTER_REWRITES}
                (id, project_id, chapter_id, rewrite_type,
                 original, rewritten, reason,
                 score_before, score_after, status,
                 created_at, updated_at, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                project_id,
                chapter_id or "",
                rewrite_type,
                original or "",
                rewritten or "",
                (reason or "").strip()[:1000],
                float(score_before or 0.0),
                float(score_after or 0.0),
                status,
                now,
                now,
                "",
            ),
        )
    return record_id


def get_rewrite(rewrite_id: str) -> dict[str, Any] | None:
    """按 id 查询单条重写记录。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_CHAPTER_REWRITES} WHERE id = ?",
            (rewrite_id,),
        ).fetchone()
    return dict(row) if row else None


def list_rewrites(
    project_id: str,
    chapter_id: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按写入顺序返回项目（可选限定章节）的重写审计记录。"""
    if chapter_id:
        rows = _query(
            f"""
            SELECT * FROM {TABLE_CHAPTER_REWRITES}
            WHERE project_id = ? AND chapter_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (project_id, chapter_id, limit),
        )
    else:
        rows = _query(
            f"""
            SELECT * FROM {TABLE_CHAPTER_REWRITES}
            WHERE project_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (project_id, limit),
        )
    return [dict(row) for row in rows]


def _query(sql: str, params: tuple[Any, ...]) -> list:
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def update_status(
    rewrite_id: str,
    status: str,
    applied_at: str = "",
) -> bool:
    """更新重写记录状态（applied/reverted 等）。返回是否命中记录。"""
    if status not in VALID_REWRITE_STATUSES:
        return False
    now = _utc_iso()
    with get_conn() as conn:
        result = conn.execute(
            f"""
            UPDATE {TABLE_CHAPTER_REWRITES}
            SET status = ?, updated_at = ?,
                applied_at = CASE WHEN ? != '' THEN ? ELSE applied_at END
            WHERE id = ?
            """,
            (status, now, applied_at, applied_at, rewrite_id),
        )
        return result.rowcount > 0


def count_rewrites(project_id: str, chapter_id: str = "") -> int:
    """统计项目（可选限定章节）的重写记录总数。"""
    if chapter_id:
        with get_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {TABLE_CHAPTER_REWRITES} "
                "WHERE project_id = ? AND chapter_id = ?",
                (project_id, chapter_id),
            ).fetchone()
    else:
        with get_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {TABLE_CHAPTER_REWRITES} "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()
    return int(row["n"]) if row else 0
