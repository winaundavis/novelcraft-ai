"""novel_facts 表 DAO（Data Access Object）—— M1 事实账本。

事实账本只记"已发生的不可逆事实"，用于长篇连载防重复事件/设定矛盾：
- completed      过程性目标/任务已完成
- revealed       重要信息/身份/秘密已揭示
- state_changed  不可逆状态变化（死亡、关系破裂、地点毁灭等）

幂等设计：project_id + content 唯一索引，重复内容自动忽略（INSERT OR IGNORE）。

记忆升级 v1（结构化扩展，与旧 schema 兼容）：
- status 状态机：active / superseded / conflict / uncertain（与 is_conflict 兼容，
  旧冲突记录 is_conflict=1 视为 status=conflict，不进入账本注入）。
- version / valid_from / valid_to / confidence：支撑验收 M3（过期状态判定）与版本追踪。
- entities_json / relations_json / source_chapter_id：结构化实体记忆 + 来源追溯（验收 M5）。
- 所有新增列写/查均做列存在性检测：老库（未迁移）与测试内存库自动降级，绝不报错。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_NOVEL_FACTS
from app.db import get_conn
from app.models.schemas import gen_id

FACT_TYPE_COMPLETED = "completed"
FACT_TYPE_REVEALED = "revealed"
FACT_TYPE_STATE_CHANGED = "state_changed"
FACT_TYPE_CONFLICT = "conflict"  # M3：写作冲突标记（仅 is_conflict=1 时使用，不进入账本注入）
VALID_FACT_TYPES = {FACT_TYPE_COMPLETED, FACT_TYPE_REVEALED, FACT_TYPE_STATE_CHANGED}

# M3：写作冲突单条记录最大长度（供人工决策阅读）
MAX_CONFLICT_LENGTH = 400

# ── 记忆升级 v1：冲突状态机（与 is_conflict 兼容） ──
FACT_STATUS_ACTIVE = "active"        # 生效（默认）
FACT_STATUS_SUPERSEDED = "superseded"  # 已被新版本取代（不再注入）
FACT_STATUS_CONFLICT = "conflict"    # 存在冲突待人工决策（等价 is_conflict=1）
FACT_STATUS_UNCERTAIN = "uncertain"  # 低置信度待核实（可注入但标记）
VALID_FACT_STATUSES = {
    FACT_STATUS_ACTIVE,
    FACT_STATUS_SUPERSEDED,
    FACT_STATUS_CONFLICT,
    FACT_STATUS_UNCERTAIN,
}


def _utc_iso() -> str:
    """带时间精度的时间戳（事实账本需要区分同日先后，用于"最近 N 条"窗口）。"""
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """列存在性检测（老库/测试内存库未迁移新列时返回 False，调用方跳过该列）。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)
    except sqlite3.Error:
        return False


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 1.0
    if confidence != confidence:
        return 1.0
    return min(1.0, max(0.0, confidence))


def _effective_status(is_conflict: bool, status: str | None) -> str:
    if is_conflict:
        return FACT_STATUS_CONFLICT
    if status in VALID_FACT_STATUSES:
        return status
    return FACT_STATUS_ACTIVE


def add_fact(
    project_id: str,
    fact_type: str,
    content: str,
    chapter_id: str = "",
    is_conflict: bool = False,
    *,
    status: str | None = None,
    version: int | None = None,
    valid_from: str = "",
    valid_to: str = "",
    confidence: float = 1.0,
    entities: list[dict[str, Any]] | None = None,
    relations: list[dict[str, Any]] | None = None,
    source_chapter_id: str = "",
) -> bool:
    """幂等写入一条事实（project_id + content 唯一，重复自动忽略）。

    M3：is_conflict=True 时标记为"写作冲突"记录，不进入账本注入。
    记忆升级 v1：结构化字段（status/version/valid_from/valid_to/confidence/
    entities_json/relations_json/source_chapter_id）在列存在时写入，否则自动跳过
    （兼容未迁移的老库与测试内存库）。
    返回是否真正插入（True=新插入，False=内容为空或已存在）。
    """
    cleaned = (content or "").strip()
    if not cleaned:
        return False
    if fact_type not in VALID_FACT_TYPES:
        fact_type = FACT_TYPE_COMPLETED
    now = _utc_iso()
    with get_conn() as conn:
        columns = [
            "id", "project_id", "fact_type", "content",
            "chapter_id", "created_at", "is_conflict",
        ]
        values: list[Any] = [
            gen_id(), project_id, fact_type, cleaned,
            chapter_id or "", now, 1 if is_conflict else 0,
        ]
        if _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            columns.append("status")
            values.append(_effective_status(is_conflict, status))
        if _has_column(conn, TABLE_NOVEL_FACTS, "version"):
            columns.append("version")
            values.append(max(1, int(version or 1)))
        if _has_column(conn, TABLE_NOVEL_FACTS, "valid_from"):
            columns.append("valid_from")
            values.append(valid_from or now)
        if _has_column(conn, TABLE_NOVEL_FACTS, "valid_to"):
            columns.append("valid_to")
            values.append(valid_to or "")
        if _has_column(conn, TABLE_NOVEL_FACTS, "confidence"):
            columns.append("confidence")
            values.append(_clamp_confidence(confidence))
        if _has_column(conn, TABLE_NOVEL_FACTS, "entities_json"):
            columns.append("entities_json")
            values.append(json.dumps(entities or [], ensure_ascii=False))
        if _has_column(conn, TABLE_NOVEL_FACTS, "relations_json"):
            columns.append("relations_json")
            values.append(json.dumps(relations or [], ensure_ascii=False))
        if _has_column(conn, TABLE_NOVEL_FACTS, "source_chapter_id"):
            columns.append("source_chapter_id")
            values.append(source_chapter_id or chapter_id or "")
        if _has_column(conn, TABLE_NOVEL_FACTS, "updated_at"):
            columns.append("updated_at")
            values.append(now)
        placeholders = ", ".join("?" for _ in columns)
        cursor = conn.execute(
            f"""
            INSERT OR IGNORE INTO {TABLE_NOVEL_FACTS}
                ({", ".join(columns)})
            VALUES ({placeholders})
            """,
            tuple(values),
        )
        return cursor.rowcount > 0


# ── 重写版本替换（验收 P1-1：重写后旧版本必须失效） ─────────────

def list_active_facts_by_chapter(
    project_id: str, chapter_id: str, *, conn=None
) -> list[dict[str, Any]]:
    """返回某章来源的 active 事实（重写版本替换的比对基准）。

    仅查 active（不含 superseded/conflict），供 replace_chapter_memory
    判断"旧设定在新版本中是否仍存在"。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_NOVEL_FACTS}
                WHERE project_id = ? AND source_chapter_id = ?
                  AND status = ? AND is_conflict = 0
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, chapter_id, FACT_STATUS_ACTIVE),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_NOVEL_FACTS}
                WHERE project_id = ? AND chapter_id = ? AND is_conflict = 0
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, chapter_id),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own_conn:
            conn.close()


def reactivate_fact_if_superseded(
    project_id: str,
    content: str,
    *,
    chapter_id: str = "",
    conn=None,
) -> bool:
    """若 content 已存在但处于 superseded，恢复为 active（重写改回原设定时"复活"）。

    背景：UNIQUE(project_id, content) + INSERT OR IGNORE —— 若某事实先被 superseded，
    后续重写又改回同 content，add_fact 会被唯一约束静默忽略，导致版本无法恢复。
    本函数负责把该记录重新激活（version+1），保证"改回原名"不丢事实。
    返回是否发生了复活（False=无 superseded 同 content 记录）。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            return False
        row = conn.execute(
            f"""
            SELECT id, version FROM {TABLE_NOVEL_FACTS}
            WHERE project_id = ? AND content = ? AND status = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (project_id, content, FACT_STATUS_SUPERSEDED),
        ).fetchone()
        if row is None:
            return False
        now = _utc_iso()
        conn.execute(
            f"""
            UPDATE {TABLE_NOVEL_FACTS}
            SET status = ?, version = ?, valid_to = '',
                updated_at = ?, source_chapter_id = ?, chapter_id = ?
            WHERE id = ?
            """,
            (
                FACT_STATUS_ACTIVE,
                max(1, int(row["version"] or 1)) + 1,
                now,
                chapter_id,
                chapter_id,
                row["id"],
            ),
        )
        if own_conn:
            conn.commit()
        return True
    finally:
        if own_conn:
            conn.close()


def supersede_facts_by_chapter(
    project_id: str,
    chapter_id: str,
    *,
    keep_contents: set[str] | None = None,
    conn=None,
) -> int:
    """把某章来源的 active 事实中、不在 keep_contents 集合内的标记为 superseded。

    用于重写版本替换：新抽取成功后再调用，仅失效"新版本不再包含"的旧设定
    （keep_contents 传入新抽取的 content 集合），未改变的事实保持 active。
    返回被标记的记录数。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            return 0
        rows = conn.execute(
            f"""
            SELECT id, content FROM {TABLE_NOVEL_FACTS}
            WHERE project_id = ? AND source_chapter_id = ?
              AND status = ? AND is_conflict = 0
            """,
            (project_id, chapter_id, FACT_STATUS_ACTIVE),
        ).fetchall()
        now = _utc_iso()
        count = 0
        for row in rows:
            # keep_contents 中的事实 = 新版本仍存在，保持 active，不失效
            if keep_contents is not None and row["content"] in keep_contents:
                continue
            cur = conn.execute(
                f"""
                UPDATE {TABLE_NOVEL_FACTS}
                SET status = ?, valid_to = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (FACT_STATUS_SUPERSEDED, now, now, row["id"]),
            )
            count += cur.rowcount
        if own_conn:
            conn.commit()
        return count
    finally:
        if own_conn:
            conn.close()


def supersede_facts_by_ids(
    project_id: str,
    fact_ids: list[str],
    *,
    conn=None,
) -> int:
    """按 id 批量把 active 事实标记为 superseded（跨章矛盾消解用）。

    与 supersede_facts_by_chapter 的区别：后者只失效"同一章来源"的旧事实，
    无法处理跨章矛盾（如新章揭示"守碑人是程老七" vs 早期"守碑人是陈九"）。
    本方法按 id 精确失效，供 LLM 检测出矛盾后调用。
    返回被标记的记录数。
    """
    if not fact_ids:
        return 0
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            return 0
        now = _utc_iso()
        count = 0
        for fid in fact_ids:
            cur = conn.execute(
                f"""
                UPDATE {TABLE_NOVEL_FACTS}
                SET status = ?, valid_to = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND project_id = ? AND status = ? AND is_conflict = 0
                """,
                (FACT_STATUS_SUPERSEDED, now, now, fid, project_id, FACT_STATUS_ACTIVE),
            )
            count += cur.rowcount
        if own_conn:
            conn.commit()
        return count
    finally:
        if own_conn:
            conn.close()


def list_facts(project_id: str) -> list[dict[str, Any]]:
    """按写入顺序返回项目全部事实。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_NOVEL_FACTS}
            WHERE project_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_completed_revealed_facts(project_id: str) -> list[dict[str, Any]]:
    """completed + revealed 事实全量返回（里程碑性事实，不限距离）。

    M3：排除写作冲突记录（is_conflict=1），避免污染账本注入。
    记忆升级 v1：排除 superseded（已被新版本取代）与 conflict 状态事实，
    防止过期状态误用（验收 M3）；uncertain 低置信度事实仍注入但保留标记。
    """
    with get_conn() as conn:
        # 兼容未迁移的老库（无 status 列）：仅按 fact_type + is_conflict 过滤
        if _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_NOVEL_FACTS}
                WHERE project_id = ? AND fact_type IN (?, ?) AND is_conflict = 0
                  AND status NOT IN (?, ?)
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, FACT_TYPE_COMPLETED, FACT_TYPE_REVEALED,
                 FACT_STATUS_SUPERSEDED, FACT_STATUS_CONFLICT),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_NOVEL_FACTS}
                WHERE project_id = ? AND fact_type IN (?, ?) AND is_conflict = 0
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, FACT_TYPE_COMPLETED, FACT_TYPE_REVEALED),
            ).fetchall()
    return [dict(row) for row in rows]


def list_recent_state_changed_facts(project_id: str, limit: int = 15) -> list[dict[str, Any]]:
    """state_changed 事实只返回最近 N 条（近因原则，控制 prompt 长度）。

    M3：排除写作冲突记录（is_conflict=1），避免污染账本注入。
    记忆升级 v1：排除 superseded/conflict 状态事实，防止过期状态误用（验收 M3）。
    """
    with get_conn() as conn:
        if _has_column(conn, TABLE_NOVEL_FACTS, "status"):
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_NOVEL_FACTS}
                WHERE project_id = ? AND fact_type = ? AND is_conflict = 0
                  AND status NOT IN (?, ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (project_id, FACT_TYPE_STATE_CHANGED,
                 FACT_STATUS_SUPERSEDED, FACT_STATUS_CONFLICT, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT * FROM {TABLE_NOVEL_FACTS}
                WHERE project_id = ? AND fact_type = ? AND is_conflict = 0
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (project_id, FACT_TYPE_STATE_CHANGED, limit),
            ).fetchall()
    return [dict(row) for row in rows]


# ── M3 写作冲突落盘 ─────────────────────────────────────

def add_writing_conflict(
    project_id: str,
    chapter_id: str,
    summary: str,
    root_cause: str = "",
) -> bool:
    """落盘一条"写作冲突"（事实核查多次失败且不可调和），供人工决策。

    - fact_type 固定为 conflict，is_conflict=1；
    - 该记录不会进入账本注入（list_completed_revealed_facts / list_recent_state_changed_facts
      均已排除 is_conflict=1），与正常事实隔离；
    - project_id + content 唯一幂等，同内容重复冲突自动忽略。
    """
    cleaned = (summary or "").strip()
    if not cleaned:
        return False
    detail = (
        f"{cleaned}（根因：{root_cause or '未知'}）"
        if root_cause
        else cleaned
    )[:MAX_CONFLICT_LENGTH]
    with get_conn() as conn:
        cursor = conn.execute(
            f"""
            INSERT OR IGNORE INTO {TABLE_NOVEL_FACTS}
                (id, project_id, fact_type, content, chapter_id, created_at, is_conflict)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                gen_id(),
                project_id,
                FACT_TYPE_CONFLICT,
                detail,
                chapter_id or "",
                _utc_iso(),
            ),
        )
        return cursor.rowcount > 0


def list_writing_conflicts(project_id: str) -> list[dict[str, Any]]:
    """返回某项目全部写作冲突记录（新→旧），供人工决策与前端展示。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_NOVEL_FACTS}
            WHERE project_id = ? AND is_conflict = 1
            ORDER BY created_at DESC, id DESC
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_writing_conflict(project_id: str, fact_id: str) -> bool:
    """删除一条写作冲突记录（P2-2b：任务中心「标记已处理」）。

    仅允许删除 is_conflict=1 的记录，防止误删正常账本事实；
    返回是否删除成功（False 表示记录不存在或不是冲突记录）。
    """
    with get_conn() as conn:
        cursor = conn.execute(
            f"""
            DELETE FROM {TABLE_NOVEL_FACTS}
            WHERE id = ? AND project_id = ? AND is_conflict = 1
            """,
            (fact_id, project_id),
        )
        return cursor.rowcount > 0


def count_facts(project_id: str) -> int:
    """统计某项目事实条数（幂等性验证用）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {TABLE_NOVEL_FACTS} WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def delete_facts_by_project(project_id: str) -> None:
    """删除某项目下的全部事实（级联删除用，SQLite 未启用外键约束时手动清理）。"""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TABLE_NOVEL_FACTS} WHERE project_id = ?", (project_id,))
