"""Deterministic Chapter Digest projection.

The digest is a compact long-form memory record, not a literary summary.  It
is derived only from Chapter Contract, observed state and immutable commit
evidence; no model or network call is permitted here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.dao import chapter_contracts_dao, chapter_commits
from app.db import get_conn
from app.models.schemas import gen_id


FIELDS = (
    "goal", "key_events", "state_changes", "character_changes",
    "relationship_changes", "knowledge_changes", "conflicts",
    "foreshadow_progress", "payoffs", "unresolved_questions", "next_direction",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([value] if value not in (None, "") else [])


def _evidence(items: Any, level: str, commit: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "value": item,
        "source_type": "chapter_commit",
        "source_id": commit.get("id", ""),
        "chapter_id": commit.get("chapter_id", ""),
        "evidence_level": level,
    } for item in _items(items)]


def _field(value: Any, level: str, commit: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": value or "",
        "source_type": "chapter_commit" if value else "chapter_contract",
        "source_id": commit.get("id", "") if value else "",
        "chapter_id": commit.get("chapter_id", ""),
        "evidence_level": level,
    }


def build_digest(commit: dict[str, Any], contract: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = contract or {}
    observed = commit.get("observed_state") or {}
    digest: dict[str, Any] = {
        "goal": _field(contract.get("goal"), "confirmed" if contract.get("goal") else "unverified", commit),
        "key_events": _evidence(commit.get("knowledge") or contract.get("key_events"), "observed" if commit.get("knowledge") else "unverified", commit),
        "state_changes": _evidence(commit.get("state_changes") or observed.get("state_changes"), "observed", commit),
        "character_changes": _evidence(observed.get("characters"), "observed", commit),
        "relationship_changes": _evidence(commit.get("relationship_changes") or observed.get("relationships"), "observed", commit),
        "knowledge_changes": _evidence(commit.get("knowledge") or observed.get("knowledge"), "observed", commit),
        "conflicts": _evidence(commit.get("conflict_changes") or observed.get("conflicts") or contract.get("conflict"), "observed" if (commit.get("conflict_changes") or observed.get("conflicts")) else "unverified", commit),
        "foreshadow_progress": _evidence(commit.get("foreshadow_changes") or observed.get("foreshadows") or contract.get("must_payoff"), "observed", commit),
        "payoffs": _evidence(commit.get("payoffs"), "confirmed" if commit.get("payoffs") else "unverified", commit),
        "unresolved_questions": _evidence(commit.get("unresolved_questions") or observed.get("mysteries") or contract.get("ending_hook"), "observed", commit),
        "next_direction": _field(commit.get("next_direction") or contract.get("goal"), "confirmed" if commit.get("next_direction") else "unverified", commit),
        "evidence": {"commit_id": commit.get("id", ""), "content_hash": commit.get("content_hash", ""), "chapter_id": commit.get("chapter_id", ""), "source": "chapter_contract+observed_state+commit", "canonical": False},
    }
    return digest


def persist_digest(commit: dict[str, Any]) -> dict[str, Any]:
    contract = chapter_contracts_dao.get_latest_contract(commit["project_id"], commit["chapter_id"]) or {}
    digest = build_digest(commit, contract)
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO chapter_digests
               (id, project_id, chapter_id, commit_id, digest_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, chapter_id) DO UPDATE SET
                 commit_id=excluded.commit_id, digest_json=excluded.digest_json,
                 updated_at=excluded.updated_at""",
            (gen_id(), commit["project_id"], commit["chapter_id"], commit["id"], json.dumps(digest, ensure_ascii=False), now, now),
        )
    return digest


def get_digest(project_id: str, chapter_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT digest_json FROM chapter_digests WHERE project_id = ? AND chapter_id = ?", (project_id, chapter_id)).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["digest_json"] or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}
