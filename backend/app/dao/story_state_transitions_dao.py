"""DAO for idempotent chapter-to-chapter Story State transitions."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_STORY_STATE_TRANSITIONS
from app.db import get_conn
from app.models.schemas import gen_id


LIST_FIELDS = (
    "knowledge_gained", "losses", "relationship_changes", "conflict_escalations",
    "foreshadow_updates", "unresolved_questions", "next_directions",
)
JSON_FIELDS = ("planned_state", "observed_state", "final_state", "before_state")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _row(row: Any) -> dict[str, Any]:
    result = {key: row[key] for key in ("id", "project_id", "chapter_id", "state_version_before", "state_version_after", "state_change", "created_at")}
    for field in LIST_FIELDS:
        try:
            value = json.loads(row[f"{field}_json"] or "[]")
        except (TypeError, ValueError):
            value = []
        result[field] = value if isinstance(value, list) else []
    result["status"] = row["status"] if "status" in row.keys() else "applied"
    for field in JSON_FIELDS:
        try:
            value = json.loads(row[f"{field}_json"] or "{}") if f"{field}_json" in row.keys() else {}
        except (TypeError, ValueError):
            value = {}
        result[field] = value if isinstance(value, dict) else {}
    try:
        conflicts = json.loads(row["conflicts_json"] or "[]") if "conflicts_json" in row.keys() else []
    except (TypeError, ValueError):
        conflicts = []
    result["conflicts"] = conflicts if isinstance(conflicts, list) else []
    return result


def get_transition(project_id: str, chapter_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_STORY_STATE_TRANSITIONS} WHERE project_id = ? AND chapter_id = ?",
            (project_id, chapter_id),
        ).fetchone()
    return _row(row) if row else None


def save_transition(project_id: str, chapter_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    existing = get_transition(project_id, chapter_id)
    if existing:
        return existing
    columns = ["id", "project_id", "chapter_id", "state_version_before", "state_version_after", "state_change", *(f"{field}_json" for field in LIST_FIELDS), "created_at"]
    params = [gen_id(), project_id, chapter_id, int(fields.get("state_version_before") or 1), int(fields.get("state_version_after") or 1), str(fields.get("state_change") or "")[:600], *(json.dumps(fields.get(field) or [], ensure_ascii=False) for field in LIST_FIELDS), _now()]
    with get_conn() as conn:
        available = {row["name"] for row in conn.execute(f"PRAGMA table_info({TABLE_STORY_STATE_TRANSITIONS})").fetchall()}
        optional = []
        optional_params: list[Any] = []
        if "status" in available:
            optional.append("status")
            optional_params.append(str(fields.get("status") or "applied")[:30])
        for field in JSON_FIELDS:
            if f"{field}_json" in available:
                optional.append(f"{field}_json")
                optional_params.append(json.dumps(fields.get(field) or {}, ensure_ascii=False))
        if "conflicts_json" in available:
            optional.append("conflicts_json")
            optional_params.append(json.dumps(fields.get("conflicts") or [], ensure_ascii=False))
        if optional:
            columns = [*columns[:-1], *optional, columns[-1]]
            params = [*params[:-1], *optional_params, params[-1]]
        conn.execute(
            f"INSERT INTO {TABLE_STORY_STATE_TRANSITIONS} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            params,
        )
    return get_transition(project_id, chapter_id)  # type: ignore[return-value]


def update_decision(project_id: str, chapter_id: str, *, status: str, final_state: dict[str, Any]) -> dict[str, Any] | None:
    """Persist a manual conflict decision without changing Story State itself."""
    if status not in {"applied", "rejected"}:
        raise ValueError("无效的状态冲突决策。")
    with get_conn() as conn:
        available = {row["name"] for row in conn.execute(f"PRAGMA table_info({TABLE_STORY_STATE_TRANSITIONS})").fetchall()}
        if "status" not in available:
            return get_transition(project_id, chapter_id)
        updates = ["status = ?"]
        params: list[Any] = [status]
        if "final_state_json" in available:
            updates.append("final_state_json = ?")
            params.append(json.dumps(final_state, ensure_ascii=False))
        params.extend([project_id, chapter_id])
        conn.execute(
            f"UPDATE {TABLE_STORY_STATE_TRANSITIONS} SET {', '.join(updates)} WHERE project_id = ? AND chapter_id = ?",
            params,
        )
    return get_transition(project_id, chapter_id)


def list_transitions(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_STORY_STATE_TRANSITIONS} WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    return [_row(row) for row in rows]
