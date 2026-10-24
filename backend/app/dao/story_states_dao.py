"""DAO for the editable project-level Story State snapshot."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_STORY_STATES
from app.db import get_conn
from app.models.schemas import gen_id

LIST_FIELDS = (
    "sub_conflicts",
    "relationship_changes",
    "current_mysteries",
    "active_foreshadows",
    "unresolved_questions",
    "promised_payoffs",
    "next_stage_directions",
)
TEXT_FIELDS = (
    "current_volume_goal",
    "current_phase_goal",
    "protagonist_goal",
    "protagonist_obstacle",
    "main_conflict",
    "last_major_state_change",
)
MAX_TEXT = 600
MAX_LIST_ITEMS = 20
MAX_ITEM = 240


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()[:MAX_TEXT]


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:MAX_ITEM] for item in value if str(item).strip()][:MAX_LIST_ITEMS]


def _row_to_dict(row: Any) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "project_id": row["project_id"],
        "source": row["source"] or "seeded",
        "version": int(row["version"] or 1),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    result.update({field: row[field] or "" for field in TEXT_FIELDS})
    for field in LIST_FIELDS:
        try:
            parsed = json.loads(row[f"{field}_json"] or "[]")
        except (TypeError, ValueError):
            parsed = []
        result[field] = parsed if isinstance(parsed, list) else []
    return result


def get_story_state(project_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_STORY_STATES} WHERE project_id = ?", (project_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_story_state(project_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    values: dict[str, Any] = {field: _clean_text(fields.get(field)) for field in TEXT_FIELDS}
    values.update({field: _clean_list(fields.get(field)) for field in LIST_FIELDS})
    state_id = gen_id()
    columns = ["id", "project_id", *TEXT_FIELDS, *(f"{field}_json" for field in LIST_FIELDS), "source", "version", "created_at", "updated_at"]
    params = [state_id, project_id, *(values[field] for field in TEXT_FIELDS), *(json.dumps(values[field], ensure_ascii=False) for field in LIST_FIELDS), fields.get("source", "seeded"), 1, now, now]
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {TABLE_STORY_STATES} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            params,
        )
    return get_story_state(project_id)  # type: ignore[return-value]


def update_story_state(project_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = set(TEXT_FIELDS) | set(LIST_FIELDS) | {"source"}
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        column = f"{key}_json" if key in LIST_FIELDS else key
        updates[column] = json.dumps(_clean_list(value), ensure_ascii=False) if key in LIST_FIELDS else _clean_text(value) if key != "source" else str(value or "author")[:40]
    if not updates:
        return get_story_state(project_id)
    updates["version"] = (get_story_state(project_id) or {}).get("version", 1) + 1
    updates["updated_at"] = _now()
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {TABLE_STORY_STATES} SET {', '.join(f'{key} = ?' for key in updates)} WHERE project_id = ?",
            [*updates.values(), project_id],
        )
    return get_story_state(project_id)
