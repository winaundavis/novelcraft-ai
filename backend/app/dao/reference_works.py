"""P2-3 参考书档案（作者的**个人**素材库）· DAO。

归属与隔离（P2-3b 起）：
- 档案归 `owner_id`（作者），**跨项目共享**：同一作者的所有书都能看到与复用；
- `project_id` 只是「来源项目」标注（哪本书期间收集的），**不是**归属，删项目不删素材；
- 与文风档案（`style_profiles`）**刻意分表**：参考档案永不进 `style_bindings`；
- `list_reference_works` 刻意**不返回 raw_text**（原文可能几十万字）；要看原文用 `get_reference_work`。

注意：本表声明了 `FOREIGN KEY ... ON DELETE CASCADE` 但本库从不开启 PRAGMA foreign_keys，
所以级联**不生效**——这是有意的（个人资产不该随项目删除而消失），并有行为测试钉住。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_REFERENCE_WORKS
from app.db import get_conn
from app.models.schemas import gen_id

MAX_NAME_LENGTH = 120
MAX_SOURCE_NOTE_LENGTH = 300
MAX_TAGS = 12                # 单个标签最长 20 字、最多 12 个
MAX_TAG_LENGTH = 20
MIN_BINDING_WEIGHT = 1
MAX_BINDING_WEIGHT = 10
DEFAULT_BINDING_WEIGHT = 6   # 界面上是「中」

_ANALYSIS_COLUMNS = (
    "char_count",
    "stored_chars",
    "truncated",
    "sample_chars",
    "sample_count",
    "status",
    "status_reason",
    "metrics",
    "dimensions",
    "narrative_rules",
    "character_rules",
    "language_rules",
    "rhythm_rules",
    "anti_ai_rules",
    "strengths",
    "weaknesses",
    "report",
    "llm_used",
)

_SUMMARY_COLUMNS = (
    "id",
    "project_id",
    "owner_id",
    "name",
    "source_note",
    "char_count",
    "stored_chars",
    "truncated",
    "sample_chars",
    "sample_count",
    "status",
    "status_reason",
    "llm_used",
    "tags",
    "created_at",
    "updated_at",
)

_JSON_COLUMNS = (
    "metrics",
    "dimensions",
    "narrative_rules",
    "character_rules",
    "language_rules",
    "rhythm_rules",
    "anti_ai_rules",
    "strengths",
    "weaknesses",
    # tags 在 create/update 里单独处理（不在 _ANALYSIS_COLUMNS 内），
    # 但读出来时必须一起解码，否则接口返回的是 JSON 字符串。
    "tags",
)

# 这些列存的是 JSON **数组**（其余 JSON 列是对象）
_LIST_JSON_COLUMNS = (
    "narrative_rules",
    "character_rules",
    "language_rules",
    "rhythm_rules",
    "anti_ai_rules",
    "strengths",
    "weaknesses",
    "tags",
)


def sanitize_tags(value: Any) -> list[str]:
    """标签规范化：去空白、去重、限长限量（非字符串一律忽略）。"""
    if isinstance(value, str):
        raw: list[Any] = [part for part in re.split(r"[,，、]\s*", value)]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return []
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()[:MAX_TAG_LENGTH]
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tags[:MAX_TAGS]


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _json_load(value: Any, fallback: Any) -> Any:
    if value in (None, "", b""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _row(row: Any, *, include_text: bool = False) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    for column in _JSON_COLUMNS:
        if column in data:
            data[column] = _json_load(
                data[column],
                [] if column in _LIST_JSON_COLUMNS else {},
            )
    data["llm_used"] = bool(data.get("llm_used"))
    data["truncated"] = bool(data.get("truncated"))
    if not include_text:
        data.pop("raw_text", None)
    return data


def create_reference_work(
    project_id: str,
    owner_id: str,
    *,
    name: str,
    raw_text: str,
    source_note: str = "",
    **analysis: Any,
) -> dict[str, Any]:
    """落库一条参考档案（归属 owner_id；project_id 仅作来源标注）。"""
    now = _utc_iso()
    record: dict[str, Any] = {
        "id": gen_id(),
        "project_id": project_id,
        "owner_id": owner_id or "local-dev",
        "name": (name or "").strip()[:MAX_NAME_LENGTH] or "未命名参考",
        "source_note": (source_note or "").strip()[:MAX_SOURCE_NOTE_LENGTH],
        "raw_text": raw_text or "",
        "tags": json.dumps(sanitize_tags(analysis.pop("tags", None)) or [], ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }
    for column in _ANALYSIS_COLUMNS:
        value = analysis.get(column)
        if column in _JSON_COLUMNS:
            record[column] = json.dumps(
                value or ([] if column in _LIST_JSON_COLUMNS else {}),
                ensure_ascii=False,
            )
        elif column in ("truncated", "llm_used"):
            record[column] = 1 if value else 0
        elif column in ("char_count", "stored_chars", "sample_chars", "sample_count"):
            record[column] = int(value or 0)
        else:
            record[column] = value if value is not None else ""
    record.setdefault("status", "completed")
    record.setdefault("status_reason", "")

    columns = list(record.keys())
    placeholders = ", ".join("?" for _ in columns)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {TABLE_REFERENCE_WORKS} ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(record[column] for column in columns),
        )
    return get_reference_work(record["owner_id"], record["id"]) or record


def list_reference_works(owner_id: str) -> list[dict[str, Any]]:
    """**该作者的全部**参考档案（跨项目），按建立时间倒序；不含原文。

    附带来源项目名，便于界面显示「来自：《某本书》」。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT {', '.join(f'r.{column}' for column in _SUMMARY_COLUMNS)},
                   COALESCE(p.name, '') AS source_project_name
            FROM {TABLE_REFERENCE_WORKS} r
            LEFT JOIN projects p ON p.id = r.project_id
            WHERE r.owner_id = ?
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (owner_id or "local-dev",),
        ).fetchall()
    return [_row(row) for row in rows]


def get_reference_work(owner_id: str, reference_id: str, *, include_text: bool = True) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT r.*, COALESCE(p.name, '') AS source_project_name
            FROM {TABLE_REFERENCE_WORKS} r
            LEFT JOIN projects p ON p.id = r.project_id
            WHERE r.owner_id = ? AND r.id = ?
            """,
            (owner_id or "local-dev", reference_id),
        ).fetchone()
    return _row(row, include_text=include_text) if row else None


def update_reference_work(
    owner_id: str,
    reference_id: str,
    *,
    name: str | None = None,
    source_note: str | None = None,
    tags: Any = None,
    **analysis: Any,
) -> dict[str, Any] | None:
    """改名/改备注/改标签，或覆盖一次重新拆解的结果（`analysis` 同 create）。"""
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = (name or "").strip()[:MAX_NAME_LENGTH] or "未命名参考"
    if source_note is not None:
        updates["source_note"] = (source_note or "").strip()[:MAX_SOURCE_NOTE_LENGTH]
    if tags is not None:
        updates["tags"] = json.dumps(sanitize_tags(tags), ensure_ascii=False)
    for column in _ANALYSIS_COLUMNS:
        if column not in analysis:
            continue
        value = analysis[column]
        if column in _JSON_COLUMNS:
            updates[column] = json.dumps(
                value or ([] if column in _LIST_JSON_COLUMNS else {}),
                ensure_ascii=False,
            )
        elif column in ("truncated", "llm_used"):
            updates[column] = 1 if value else 0
        elif column in ("char_count", "stored_chars", "sample_chars", "sample_count"):
            updates[column] = int(value or 0)
        else:
            updates[column] = value if value is not None else ""

    if not updates:
        return get_reference_work(owner_id, reference_id)
    updates["updated_at"] = _utc_iso()
    set_clause = ", ".join(f"{column} = ?" for column in updates)
    params = [updates[column] for column in updates]
    params.extend([owner_id or "local-dev", reference_id])
    with get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE {TABLE_REFERENCE_WORKS} SET {set_clause} WHERE owner_id = ? AND id = ?",
            tuple(params),
        )
        if cursor.rowcount <= 0:
            return None
    return get_reference_work(owner_id, reference_id)


def delete_reference_work(owner_id: str, reference_id: str) -> bool:
    """删除档案，并清理**任何项目**里指向它的启用绑定（避免悬空）。"""
    with get_conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM {TABLE_REFERENCE_WORKS} WHERE owner_id = ? AND id = ?",
            (owner_id or "local-dev", reference_id),
        )
        deleted = cursor.rowcount > 0
        if deleted:
            conn.execute("DELETE FROM reference_bindings WHERE reference_id = ?", (reference_id,))
        return deleted


def delete_reference_works_by_project(project_id: str) -> None:
    """按「来源项目」清理（仅测试/维护用；**项目删除不调用它**，素材是个人资产）。"""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TABLE_REFERENCE_WORKS} WHERE project_id = ?", (project_id,))


# ── 「正在参考哪些」绑定（项目级，可多份 + 强度；与 style_bindings 无关） ──

def list_bindings(project_id: str) -> list[dict[str, Any]]:
    """本项目启用的参考档案，按**强度降序**（同强度按加入时间）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reference_bindings
            WHERE project_id = ? AND reference_id != ''
            ORDER BY weight DESC, created_at ASC, id ASC
            """,
            (project_id,),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def replace_bindings(project_id: str, items: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """整体替换本项目的启用集合（原子：先清后插）。`items` 为 (reference_id, weight)。

    强度会被夹到 [1, 10]；同一档案重复出现只保留最高强度那条。
    """
    now = _utc_iso()
    normalized: dict[str, int] = {}
    for reference_id, weight in items:
        rid = str(reference_id or "").strip()
        if not rid:
            continue
        raw_weight = DEFAULT_BINDING_WEIGHT if weight is None else weight
        try:
            numeric = int(raw_weight)
        except (TypeError, ValueError):
            numeric = DEFAULT_BINDING_WEIGHT
        clamped = max(MIN_BINDING_WEIGHT, min(MAX_BINDING_WEIGHT, numeric))
        normalized[rid] = max(normalized.get(rid, 0), clamped)
    with get_conn() as conn:
        conn.execute("DELETE FROM reference_bindings WHERE project_id = ?", (project_id,))
        for reference_id, weight in normalized.items():
            conn.execute(
                """
                INSERT INTO reference_bindings(id, project_id, reference_id, weight, scope, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'project', ?, ?)
                """,
                (gen_id(), project_id, reference_id, weight, now, now),
            )
    return list_bindings(project_id)


def get_binding(project_id: str) -> dict[str, Any] | None:
    """兼容旧调用：返回强度最高的那一条（没有则 None）。"""
    bindings = list_bindings(project_id)
    return bindings[0] if bindings else None


def clear_binding(project_id: str) -> None:
    replace_bindings(project_id, [])
