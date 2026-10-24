"""DAO for versioned deterministic Chapter Contracts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_CHAPTER_CONTRACTS
from app.db import get_conn
from app.models.schemas import gen_id

LIST_FIELDS = ("characters", "key_events", "must_payoff", "must_avoid")
TEXT_FIELDS = ("goal", "conflict", "state_change", "ending_hook", "pacing")
OPTIONAL_TEXT_FIELDS = ("chapter_mode",)
OPTIONAL_JSON_FIELDS = ("arc_context",)
OPTIONAL_INT_FIELDS = ("story_state_version",)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:240] for item in value if str(item).strip()][:20]


def _row_to_dict(row: Any) -> dict[str, Any]:
    result = {field: row[field] or "" for field in TEXT_FIELDS}
    result.update({"id": row["id"], "project_id": row["project_id"], "chapter_id": row["chapter_id"], "version": int(row["version"] or 1), "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"]})
    for field in LIST_FIELDS:
        try:
            value = json.loads(row[f"{field}_json"] or "[]")
        except (TypeError, ValueError):
            value = []
        result[field] = value if isinstance(value, list) else []
    if "chapter_mode" in row.keys():
        result["chapter_mode"] = row["chapter_mode"] or ""
    if "arc_context_json" in row.keys():
        try:
            value = json.loads(row["arc_context_json"] or "{}")
        except (TypeError, ValueError):
            value = {}
        result["arc_context"] = value if isinstance(value, dict) else {}
    if "story_state_version" in row.keys():
        result["story_state_version"] = int(row["story_state_version"] or 0)
    return result


def get_latest_contract(project_id: str, chapter_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_CHAPTER_CONTRACTS} WHERE project_id = ? AND chapter_id = ? ORDER BY version DESC LIMIT 1",
            (project_id, chapter_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def save_contract(project_id: str, chapter_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    existing = get_latest_contract(project_id, chapter_id)
    comparable = {field: str(fields.get(field) or "") for field in TEXT_FIELDS}
    comparable.update({field: _clean_list(fields.get(field)) for field in LIST_FIELDS})
    comparable["chapter_mode"] = str(fields.get("chapter_mode") or "")
    comparable["arc_context"] = fields.get("arc_context") if isinstance(fields.get("arc_context"), dict) else {}
    comparable["story_state_version"] = max(0, int(fields.get("story_state_version") or 0))
    if existing and all(existing.get(key) == value for key, value in comparable.items() if key in existing):
        return existing
    version = int(existing["version"]) + 1 if existing else 1
    now = _now()
    with get_conn() as conn:
        available = {row["name"] for row in conn.execute(f"PRAGMA table_info({TABLE_CHAPTER_CONTRACTS})").fetchall()}
        optional = [field for field in OPTIONAL_TEXT_FIELDS if field in available]
        optional_json = [field for field in OPTIONAL_JSON_FIELDS if f"{field}_json" in available]
        optional_int = [field for field in OPTIONAL_INT_FIELDS if field in available]
        columns = ["id", "project_id", "chapter_id", "version", "status", *TEXT_FIELDS, *(f"{field}_json" for field in LIST_FIELDS), *optional, *(f"{field}_json" for field in optional_json), *optional_int, "created_at", "updated_at"]
        params = [gen_id(), project_id, chapter_id, version, "compiled", *(comparable[field] for field in TEXT_FIELDS), *(json.dumps(comparable[field], ensure_ascii=False) for field in LIST_FIELDS), *(comparable[field] for field in optional), *(json.dumps(comparable[field], ensure_ascii=False) for field in optional_json), *(comparable[field] for field in optional_int), now, now]
        conn.execute(
            f"INSERT INTO {TABLE_CHAPTER_CONTRACTS} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            params,
        )
    return get_latest_contract(project_id, chapter_id)  # type: ignore[return-value]


def list_latest_contracts(project_id: str) -> list[dict[str, Any]]:
    """Return one newest contract per chapter for deterministic dashboards."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.* FROM {TABLE_CHAPTER_CONTRACTS} c
            JOIN (SELECT chapter_id, MAX(version) AS version FROM {TABLE_CHAPTER_CONTRACTS}
                  WHERE project_id = ? GROUP BY chapter_id) latest
              ON latest.chapter_id = c.chapter_id AND latest.version = c.version
            WHERE c.project_id = ? ORDER BY c.chapter_id
            """,
            (project_id, project_id),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
