"""novel_jobs 表 DAO（Data Access Object）—— 封装所有对 novel_jobs 表的 SQL 操作。"""

from __future__ import annotations

from typing import Any

from app.dao.constants import TABLE_NOVEL_JOBS
from app.db import get_conn


def get_job(job_id: str) -> dict[str, Any] | None:
    """按主键查询单个长篇创作任务（返回原始 sqlite3.Row 转换的 dict）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_NOVEL_JOBS} WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def list_jobs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """获取某项目的历史创作任务列表，按创建时间倒序排列。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_NOVEL_JOBS}
            WHERE project_id = ?
            ORDER BY created_at DESC, updated_at DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_latest_project_job(project_id: str, *, running_only: bool = False) -> dict[str, Any] | None:
    """获取项目的最新一条创作任务（可选只查运行中的）。"""
    query = f"SELECT * FROM {TABLE_NOVEL_JOBS} WHERE project_id = ?"
    params: list[Any] = [project_id]
    if running_only:
        query += " AND status IN ('queued', 'running')"
    query += " ORDER BY created_at DESC, updated_at DESC LIMIT 1"
    with get_conn() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
    return dict(row) if row else None


def save_job(job_fields: dict[str, Any]) -> None:
    """插入或更新一条创作任务记录（UPSERT）。"""
    columns = list(job_fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = tuple(job_fields[col] for col in columns)

    update_pairs = [f"{col} = excluded.{col}" for col in columns]
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TABLE_NOVEL_JOBS} ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET
                {', '.join(update_pairs)}
            """,
            values,
        )


def get_running_job_ids() -> list[str]:
    """获取所有处于 queued/running 状态（含可恢复 interrupted）的 job id 列表。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id FROM {TABLE_NOVEL_JOBS} WHERE status IN ('queued', 'running', 'interrupted')"
        ).fetchall()
    return [row["id"] for row in rows]


def cancel_running_jobs(message: str, now_iso: str) -> int:
    """将所有 queued/running 状态的任务标记为 cancelled，返回受影响行数。"""
    with get_conn() as conn:
        result = conn.execute(
            f"""
            UPDATE {TABLE_NOVEL_JOBS}
            SET status = 'cancelled',
                cancel_requested = 1,
                message = ?,
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (message, now_iso, now_iso),
        )
        return result.rowcount


def mark_interrupted_jobs(message: str, now_iso: str) -> int:
    """M2：后端重启时，将所有 queued/running 状态的任务标记为 interrupted（可恢复），
    返回受影响行数。区别于 cancel_running_jobs：不置 finished_at、不置 cancel_requested，
    保留 checkpoint 信息以便前端展示"可恢复"并提供恢复入口。"""
    with get_conn() as conn:
        result = conn.execute(
            f"""
            UPDATE {TABLE_NOVEL_JOBS}
            SET status = 'interrupted',
                message = ?,
                updated_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (message, now_iso),
        )
        return result.rowcount


def list_recoverable_jobs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """获取某项目处于可恢复状态（interrupted，或含失败章待处理）的历史任务，
    按创建时间倒序排列。供前端任务中心展示"可恢复"入口。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_NOVEL_JOBS}
            WHERE project_id = ?
              AND (status = 'interrupted' OR pending_manual_recovery = 1)
            ORDER BY created_at DESC, updated_at DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]
