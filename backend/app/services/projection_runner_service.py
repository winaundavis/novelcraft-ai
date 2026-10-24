"""Deterministic, retryable projections derived from immutable chapter commits.

This runner deliberately never imports an LLM service.  A failed projection is
recorded independently and can be retried from the accepted commit snapshot.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.dao import chapter_commits as commits
from app.dao import novel_facts, story_states_dao
from app.dao import memory_fts
from app.db import DATA_DIR, get_conn
from app.services import chapter_digest_service, story_state_transition_service

WRITERS = ("story_state", "memory", "summary", "index", "foreshadow")
LEGACY_WRITER_NAMES = {
    "chapter_summary": "summary", "facts": "memory", "entities": "memory",
    "volume": "memory", "fts": "index",
}
LOG_PATH = Path(os.getenv("NOVELCRAFT_PROJECTION_LOG_PATH", str(DATA_DIR / "projection_log.jsonl")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_log(entry: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _set_status(project_id: str, chapter_id: str, writer: str, status: str, detail: str = "", commit_id: str = "") -> None:
    commits.set_projection(project_id=project_id, chapter_id=chapter_id, projection_type=writer, status=status, detail=detail, commit_id=commit_id)
    # Legacy projection names remain a read-only compatibility adapter while
    # all new orchestration and logs use the canonical vocabulary.
    aliases = {
        "summary": ("chapter_summary",),
        "memory": ("facts", "entities", "volume"),
        "index": ("fts",),
    }
    for alias in aliases.get(writer, ()):
        commits.set_projection(project_id=project_id, chapter_id=chapter_id, projection_type=alias, status=status, detail=detail, commit_id=commit_id)


def _apply_memory(commit: dict[str, Any]) -> None:
    for raw in commit["observed_facts"]:
        if isinstance(raw, dict):
            text = str(raw.get("content") or raw.get("fact") or "").strip()
            kind = str(raw.get("type") or "completed")
        else:
            text, kind = str(raw).strip(), "completed"
        if text:
            novel_facts.reactivate_fact_if_superseded(commit["project_id"], text, chapter_id=commit["chapter_id"])
            novel_facts.add_fact(commit["project_id"], kind, text, commit["chapter_id"], source_chapter_id=commit["chapter_id"])


def _apply_summary(commit: dict[str, Any]) -> None:
    chapter_digest_service.persist_digest(commit)


def _apply_story_state(commit: dict[str, Any]) -> None:
    if story_states_dao.get_story_state(commit["project_id"]) is None:
        raise RuntimeError("story state is missing")
    evidence = {
        "characters": [],
        "relationships": commit.get("relationship_changes") or [],
        "knowledge": commit.get("knowledge_changes") or commit.get("observed_facts") or [],
        "resources": [],
        "conflicts": commit.get("conflict_changes") or [],
        "foreshadows": commit.get("foreshadow_changes") or [],
        "mysteries": commit.get("unresolved_questions") or [],
        "next_goals": [commit["next_direction"]] if commit.get("next_direction") else [],
        "state_changes": commit.get("state_changes") or [],
        "evidence": {"source": "chapter_commit", "commit_id": commit["id"]},
    }
    transition = story_state_transition_service.apply_chapter_transition(
        commit["project_id"], commit["chapter_id"],
        summary="", content=commit.get("content_snapshot") or "", observed_evidence=evidence,
    )
    if transition.get("status") == "conflict":
        raise RuntimeError("story state transition requires manual conflict resolution")


def _apply_foreshadow(commit: dict[str, Any]) -> None:
    # Existing foreshadow records are author-owned.  Commit data is retained as
    # evidence; this projection is intentionally a no-op until an author accepts it.
    return None


def _apply_index(commit: dict[str, Any]) -> None:
    if not memory_fts.sync_chapter_fts(commit["project_id"], commit["chapter_id"]):
        raise RuntimeError("FTS index is unavailable")


_HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "story_state": _apply_story_state,
    "memory": _apply_memory,
    "summary": _apply_summary,
    "index": _apply_index,
    "foreshadow": _apply_foreshadow,
}


def run_projection(commit_id: str, writer: str, *, retry_of: str = "") -> dict[str, Any]:
    writer = LEGACY_WRITER_NAMES.get(writer, writer)
    if writer not in WRITERS:
        raise ValueError(f"unknown projection writer: {writer}")
    commit = commits.get_commit_by_id(commit_id)
    if commit is None:
        raise ValueError("chapter commit not found")
    canonical = commits.get_canonical_commit(commit["project_id"], commit["chapter_id"])
    if commit.get("status", "accepted") != "accepted" or not canonical or canonical["id"] != commit_id:
        raise ValueError("chapter commit is no longer canonical")
    if not commits.content_matches_commit(commit["project_id"], commit["chapter_id"]):
        raise ValueError("active chapter content does not match canonical commit")
    run_id, started = uuid.uuid4().hex, _now()
    result: dict[str, Any] = {"run_id": run_id, "commit_id": commit_id, "writer": writer, "status": "failed"}
    try:
        _HANDLERS[writer](commit)
        _set_status(commit["project_id"], commit["chapter_id"], writer, "done", commit_id=commit_id)
        result["status"] = "done"
    except Exception as exc:  # preserve the commit even when a projection fails
        detail = str(exc)[:240]
        _set_status(commit["project_id"], commit["chapter_id"], writer, "failed", detail, commit_id=commit_id)
        result["error"] = detail
    finally:
        _append_log({
            "run_id": run_id, "chapter_id": commit["chapter_id"], "commit_id": commit_id,
            "commit_hash": commit["content_hash"], "writer": writer, "status": result["status"],
            "started_at": started, "finished_at": _now(), "error": result.get("error", ""), "retry_of": retry_of,
        })
    return result


def retry_failed_projection(project_id: str, chapter_id: str, writer: str, *, retry_of: str = "") -> dict[str, Any]:
    commit = commits.get_commit(project_id, chapter_id)
    if commit is None:
        raise ValueError("accepted chapter commit not found")
    return run_projection(commit["id"], writer, retry_of=retry_of)


def replay_project(project_id: str, *, writers: tuple[str, ...] = WRITERS) -> list[dict[str, Any]]:
    """Replay exactly one canonical commit per chapter in chapter order."""
    canonical_commits = commits.list_canonical_commits(project_id)
    return [run_projection(commit["id"], writer, retry_of="replay") for commit in canonical_commits for writer in writers]
