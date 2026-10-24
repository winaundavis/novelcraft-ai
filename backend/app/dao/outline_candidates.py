"""Persistence for user-confirmed project outline change candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db import get_conn, json_dump
from app.models.schemas import gen_id


VALID_STATUSES = {"proposed", "applied", "rejected", "reverted", "conflicted"}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_value(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["candidate_outline_json"] = _json_value(item.get("candidate_outline_json"), {})
    item["previous_outline_json"] = _json_value(item.get("previous_outline_json"), None)
    item["diff_json"] = _json_value(item.get("diff_json"), {})
    item["base_outline_version"] = int(item.get("base_outline_version") or 1)
    return item


def create_outline_candidate(
    *,
    project_id: str,
    user_id: str,
    run_id: str,
    base_outline_hash: str,
    base_outline_version: int,
    candidate_outline_json: dict[str, Any],
    previous_outline_json: dict[str, Any] | None,
    diff_json: dict[str, Any],
    instruction: str,
    status: str = "proposed",
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError("invalid outline candidate status")
    now = _utc_iso()
    candidate_id = gen_id()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO outline_candidates (
                id, project_id, user_id, run_id, base_outline_hash,
                base_outline_version, candidate_outline_json,
                previous_outline_json, diff_json, instruction, status,
                created_at, updated_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                project_id,
                user_id,
                run_id or "",
                base_outline_hash,
                max(1, int(base_outline_version or 1)),
                json_dump(candidate_outline_json),
                json_dump(previous_outline_json) if previous_outline_json is not None else None,
                json_dump(diff_json),
                (instruction or "").strip()[:2000],
                status,
                now,
                now,
                now if status == "applied" else None,
            ),
        )
    created = get_outline_candidate(candidate_id)
    if created is None:  # pragma: no cover - SQLite insert/read invariant
        raise RuntimeError("outline candidate insert failed")
    return created


def get_outline_candidate(candidate_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM outline_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
    return _row(row)


def list_outline_candidates(
    project_id: str,
    user_id: str,
    status: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM outline_candidates WHERE project_id = ? AND user_id = ?"
    params: list[Any] = [project_id, user_id]
    if status is not None:
        if status not in VALID_STATUSES:
            return []
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row(row) for row in rows if row is not None]


def update_outline_candidate_status(
    candidate_id: str,
    status: str,
    previous_outline_json: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if status not in VALID_STATUSES:
        raise ValueError("invalid outline candidate status")
    now = _utc_iso()
    with get_conn() as conn:
        if previous_outline_json is None:
            conn.execute(
                """
                UPDATE outline_candidates
                SET status = ?, updated_at = ?,
                    applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END
                WHERE id = ?
                """,
                (status, now, status, now, candidate_id),
            )
        else:
            conn.execute(
                """
                UPDATE outline_candidates
                SET status = ?, previous_outline_json = ?, updated_at = ?,
                    applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END
                WHERE id = ?
                """,
                (status, json_dump(previous_outline_json), now, status, now, candidate_id),
            )
    return get_outline_candidate(candidate_id)
