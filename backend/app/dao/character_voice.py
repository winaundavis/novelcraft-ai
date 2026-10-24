"""DAO for structured, project-local character voice profiles."""

from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, utc_today
from app.models.schemas import gen_id

VOICE_FIELDS = ("worldview", "speech_length", "sentence_pattern", "favorite_expressions", "avoided_expressions", "emotional_expression", "information_strategy", "behavior_signature")


def _row(row: Any) -> dict[str, Any]:
    result = {"id": row["id"], "project_id": row["project_id"], "character_id": row["character_id"], "name": row["name"] or ""}
    for field in VOICE_FIELDS:
        key = f"{field}_json"
        try:
            value = json.loads(row[key] or ("[]" if field.endswith("expressions") else '""'))
        except (TypeError, ValueError):
            value = [] if field.endswith("expressions") else ""
        result[field] = value
    return result


def list_profiles(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM character_voice_profiles WHERE project_id = ? ORDER BY name", (project_id,)).fetchall()
    return [_row(row) for row in rows]


def create_profile(project_id: str, character_id: str, name: str, fields: dict[str, Any]) -> dict[str, Any]:
    now = utc_today()
    columns = ["id", "project_id", "character_id", "name", *(f"{field}_json" for field in VOICE_FIELDS), "created_at", "updated_at"]
    values = [gen_id(), project_id, character_id, name, *[json.dumps(fields.get(field, [] if field.endswith("expressions") else ""), ensure_ascii=False) for field in VOICE_FIELDS], now, now]
    with get_conn() as conn:
        conn.execute(f"INSERT INTO character_voice_profiles ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)
    return next(item for item in list_profiles(project_id) if item["character_id"] == character_id)


def update_profile(project_id: str, character_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    updates = {f"{field}_json": json.dumps(fields[field], ensure_ascii=False) for field in VOICE_FIELDS if field in fields}
    if not updates:
        return next((item for item in list_profiles(project_id) if item["character_id"] == character_id), None)
    updates["updated_at"] = utc_today()
    with get_conn() as conn:
        conn.execute(f"UPDATE character_voice_profiles SET {', '.join(f'{key} = ?' for key in updates)} WHERE project_id = ? AND character_id = ?", [*updates.values(), project_id, character_id])
    return next((item for item in list_profiles(project_id) if item["character_id"] == character_id), None)


def delete_profiles_by_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM character_voice_profiles WHERE project_id = ?", (project_id,))
