"""memory_recall_log 表 DAO（M1 增强：记忆召回日志，验收 M1/M8）。

每次生成实际注入了哪些记忆块（块名/来源 ID/字符数）都落一条日志：
- 支撑验收 M1（召回率测量：哪些事实/摘要被召回）与 M8（上下文长度实测）的自动化统计。
- 写入幂等、降级：表不存在 / 参数为空时静默跳过，绝不阻塞生成任务。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_MEMORY_RECALL_LOG
from app.db import get_conn
from app.models.schemas import gen_id


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _table_exists(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (TABLE_MEMORY_RECALL_LOG,),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def log_recall(
    project_id: str,
    chapter_id: str,
    *,
    recall_type: str = "generation",
    block_key: str = "",
    source_ids: list[str] | None = None,
    chars: int = 0,
    conn=None,
) -> bool:
    """写入一条记忆召回日志；返回是否真正写入（False = 表缺失 / 无内容）。

    block_key 示例：volume_summaries / recent_chapter_summaries / last_chapter_ending /
    completed_milestones / fts_results / entity_state / facts。
    """
    if not project_id or chars <= 0:
        return False
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _table_exists(conn):
            return False
        conn.execute(
            f"""
            INSERT INTO {TABLE_MEMORY_RECALL_LOG}
                (id, project_id, chapter_id, recall_type, block_key,
                 source_ids, chars, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gen_id(),
                project_id,
                chapter_id or "",
                recall_type,
                block_key,
                json.dumps(source_ids or [], ensure_ascii=False),
                int(chars),
                _utc_iso(),
            ),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚（召回日志写不进去）
        if own_conn:
            conn.commit()
        return True
    except Exception:  # noqa: BLE001  降级：日志写入失败不阻塞生成
        return False
    finally:
        if own_conn:
            conn.close()


def list_recalls(
    project_id: str,
    *,
    chapter_id: str | None = None,
    limit: int = 200,
    conn=None,
) -> list[dict[str, Any]]:
    """查询召回日志（新→旧）；按项目 / 章节过滤，支撑验收统计。"""
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    if chapter_id:
        where.append("chapter_id = ?")
        params.append(chapter_id)
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_MEMORY_RECALL_LOG}
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def recall_stats(project_id: str, conn=None) -> dict[str, Any]:
    """聚合召回统计（支撑验收 M8 上下文实测报告）。

    返回：总日志条数、注入字符数合计、各 block_key 的条数/字符数、按章节聚合数。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(chars), 0) AS chars
            FROM {TABLE_MEMORY_RECALL_LOG} WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        by_block = conn.execute(
            f"""
            SELECT block_key, COUNT(*) AS n, COALESCE(SUM(chars), 0) AS chars
            FROM {TABLE_MEMORY_RECALL_LOG} WHERE project_id = ?
            GROUP BY block_key ORDER BY chars DESC
            """,
            (project_id,),
        ).fetchall()
        by_chapter = conn.execute(
            f"""
            SELECT chapter_id, COUNT(*) AS n, COALESCE(SUM(chars), 0) AS chars
            FROM {TABLE_MEMORY_RECALL_LOG} WHERE project_id = ? AND chapter_id != ''
            GROUP BY chapter_id ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
        return {
            "total_logs": int(total["n"]) if total else 0,
            "total_chars": int(total["chars"]) if total else 0,
            "by_block": [dict(r) for r in by_block],
            "by_chapter": [dict(r) for r in by_chapter],
        }
    except Exception:  # noqa: BLE001  表缺失时降级返回空统计
        return {"total_logs": 0, "total_chars": 0, "by_block": [], "by_chapter": []}
    finally:
        if own_conn:
            conn.close()
