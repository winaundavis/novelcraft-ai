"""DAO for reviewed project writing experiences."""

from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, utc_today
from app.models.schemas import gen_id


def _row(row: Any) -> dict[str, Any]:
    try:
        chapters = json.loads(row["source_chapters_json"] or "[]")
    except (TypeError, ValueError):
        chapters = []
    return {"id": row["id"], "project_id": row["project_id"], "experience_type": row["experience_type"], "observation": row["observation"], "preferred_fix": row["preferred_fix"], "evidence_count": int(row["evidence_count"] or 0), "status": row["status"], "source_chapters": chapters if isinstance(chapters, list) else [], "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_experiences(project_id: str, status: str = "") -> list[dict[str, Any]]:
    where = "project_id = ?"
    params: list[Any] = [project_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM writing_experiences WHERE {where} ORDER BY updated_at DESC", params).fetchall()
    return [_row(row) for row in rows]


def list_accepted(project_id: str) -> list[dict[str, Any]]:
    return list_experiences(project_id, "accepted")


def record_observation(project_id: str, experience_type: str, observation: str, preferred_fix: str, source_chapters: list[str]) -> dict[str, Any]:
    now = utc_today()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM writing_experiences WHERE project_id = ? AND experience_type = ? AND observation = ?", (project_id, experience_type, observation)).fetchone()
        if row:
            merged = sorted(set(json.loads(row["source_chapters_json"] or "[]") + source_chapters))[:30]
            conn.execute("UPDATE writing_experiences SET evidence_count = evidence_count + 1, source_chapters_json = ?, preferred_fix = ?, updated_at = ? WHERE id = ?", (json.dumps(merged, ensure_ascii=False), preferred_fix, now, row["id"]))
        else:
            conn.execute("INSERT INTO writing_experiences (id, project_id, experience_type, observation, preferred_fix, evidence_count, status, source_chapters_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 'candidate', ?, ?, ?)", (gen_id(), project_id, experience_type, observation, preferred_fix, json.dumps(source_chapters[:30], ensure_ascii=False), now, now))
    return next(item for item in list_experiences(project_id) if item["experience_type"] == experience_type and item["observation"] == observation)


def accept(experience_id: str, project_id: str = "") -> dict[str, Any] | None:
    with get_conn() as conn:
        scope = " AND project_id = ?" if project_id else ""
        params = (experience_id, project_id) if project_id else (experience_id,)
        row = conn.execute(f"SELECT * FROM writing_experiences WHERE id = ?{scope}", params).fetchone()
        if not row or int(row["evidence_count"] or 0) < 3:
            return _row(row) if row else None
        conn.execute(f"UPDATE writing_experiences SET status = 'accepted', updated_at = ? WHERE id = ?{scope}", (utc_today(), *params))
        row = conn.execute(f"SELECT * FROM writing_experiences WHERE id = ?{scope}", params).fetchone()
    return _row(row) if row else None


def reject(experience_id: str, project_id: str = "") -> dict[str, Any] | None:
    with get_conn() as conn:
        scope = " AND project_id = ?" if project_id else ""
        params = (experience_id, project_id) if project_id else (experience_id,)
        conn.execute(f"UPDATE writing_experiences SET status = 'rejected', updated_at = ? WHERE id = ?{scope}", (utc_today(), *params))
        row = conn.execute(f"SELECT * FROM writing_experiences WHERE id = ?{scope}", params).fetchone()
    return _row(row) if row else None


def delete_by_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM writing_experiences WHERE project_id = ?", (project_id,))
