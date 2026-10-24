"""Remove chapter-derived memory inside the chapter recycle transaction.

No model calls and no reset of author-owned Story State. Legacy state without
before snapshots can only be reconciled from its recorded source contributions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.dao import chapter_commits, memory_fts


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "null")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _values(value: Any) -> set[str]:
    return {item for item in value if isinstance(item, str) and item} if isinstance(value, list) else set()


def _contributions(transition: dict[str, Any]) -> dict[str, set[str]]:
    observed = _json(transition.get("observed_state_json"), {})
    planned = _json(transition.get("planned_state_json"), {})
    planned_mysteries = planned.get("mysteries") or {}
    if isinstance(planned_mysteries, dict):
        planned_mysteries = planned_mysteries.get("value", [])
    mysteries = _values(observed.get("mysteries"))
    return {
        "relationship_changes": _values(observed.get("relationships")) | _values(_json(transition.get("relationship_changes_json"), [])),
        "current_mysteries": mysteries,
        "unresolved_questions": mysteries | _values(planned_mysteries),
    }


def _reconcile_state(conn: Any, project_id: str, removed: dict[str, Any], snapshot: dict[str, Any]) -> None:
    if removed.get("status", "applied") != "applied":
        return
    state_row = conn.execute("SELECT * FROM story_states WHERE project_id=?", (project_id,)).fetchone()
    if not state_row:
        return
    state = dict(state_row)
    snapshot["story_state"] = state
    others = [dict(row) for row in conn.execute(
        "SELECT t.* FROM story_state_transitions t JOIN chapters c ON c.id=t.chapter_id AND c.project_id=t.project_id "
        "WHERE t.project_id=? AND t.chapter_id!=? AND t.status='applied' ORDER BY t.state_version_after",
        (project_id, removed["chapter_id"]),
    ).fetchall()]
    before = _json(removed.get("before_state_json"), {})
    contributions = _contributions(removed)
    changes: dict[str, Any] = {}
    removed_values: dict[str, set[str]] = {}
    authored_values: dict[str, set[str]] = {}
    for field, values in contributions.items():
        surviving = set().union(*(_contributions(item)[field] for item in others))
        previous_sources = set().union(*(
            _contributions(item)[field] for item in others
            if item["state_version_after"] <= removed["state_version_before"]
        ))
        authored = _values(before.get(field)) - previous_sources
        authored_values[field] = authored
        discard = values - surviving - authored
        removed_values[field] = discard
        column = field + "_json"
        original = _json(state.get(column), [])
        cleaned = [item for item in original if item not in discard]
        if cleaned != original:
            changes[column] = json.dumps(cleaned, ensure_ascii=False)

    scalar = str(removed.get("state_change") or "")
    if scalar and state.get("last_major_state_change") == scalar and before.get("last_major_state_change") != scalar:
        replacements = [str(item.get("state_change") or "") for item in others if item.get("state_change")]
        changes["last_major_state_change"] = replacements[-1] if replacements else str(before.get("last_major_state_change") or "")
    directions = _json(removed.get("next_directions_json"), [])
    if directions and _json(state.get("next_stage_directions_json"), []) == directions and before.get("next_stage_directions") != directions:
        replacements = [
            _json(item.get("next_directions_json"), []) for item in others
            if _json(item.get("next_directions_json"), []) != directions
        ]
        changes["next_stage_directions_json"] = json.dumps(replacements[-1] if replacements else before.get("next_stage_directions", []), ensure_ascii=False)

    # Later before snapshots contain this chapter's inherited contributions.
    # Scrub that inheritance even when a later chapter independently contributes
    # the same value, so recycling the last source can remove it too.
    for item in others:
        patch: dict[str, str] = {}
        for column in ("before_state_json", "final_state_json"):
            if column not in item:
                continue
            old = _json(item.get(column), {})
            cleaned = dict(old)
            for field, values in contributions.items():
                discard = removed_values[field]
                if column == "before_state_json" and item["state_version_before"] >= removed["state_version_after"]:
                    discard = values - authored_values[field]
                if isinstance(cleaned.get(field), list):
                    cleaned[field] = [value for value in cleaned[field] if value not in discard]
            if scalar and cleaned.get("last_major_state_change") == scalar and before.get("last_major_state_change") != scalar:
                cleaned["last_major_state_change"] = str(before.get("last_major_state_change") or "")
            if directions and cleaned.get("next_stage_directions") == directions and before.get("next_stage_directions") != directions:
                cleaned["next_stage_directions"] = list(before.get("next_stage_directions") or [])
            if cleaned != old:
                patch[column] = json.dumps(cleaned, ensure_ascii=False)
        if patch:
            snapshot.setdefault("adjusted_transitions", []).append(item)
            conn.execute(f"UPDATE story_state_transitions SET {', '.join(key + '=?' for key in patch)} WHERE id=? AND project_id=?", (*patch.values(), item["id"], project_id))
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(f"UPDATE story_states SET {', '.join(key + '=?' for key in changes)}, version=version+1 WHERE project_id=?", (*changes.values(), project_id))


def invalidate_chapter_on_conn(conn: Any, project_id: str, chapter_id: str) -> dict[str, Any]:
    """Archive and invalidate derived records; caller owns commit/rollback."""
    snapshot: dict[str, Any] = {}
    chapter = conn.execute("SELECT * FROM chapters WHERE project_id=? AND id=?", (project_id, chapter_id)).fetchone()
    if chapter is None:
        return snapshot
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for table, predicate, params in (
        ("novel_facts", "(source_chapter_id=? OR (COALESCE(source_chapter_id,'')='' AND chapter_id=?))", (chapter_id, chapter_id)),
        ("memory_entities", "source_chapter_id=?", (chapter_id,)),
        ("memory_relations", "source_chapter_id=?", (chapter_id,)),
    ):
        rows = conn.execute(f"SELECT * FROM {table} WHERE project_id=? AND {predicate}", (project_id, *params)).fetchall()
        snapshot[table] = [dict(row) for row in rows]
        conn.execute(
            f"UPDATE {table} SET status='superseded', valid_to=?, updated_at=?, version=version+1 "
            f"WHERE project_id=? AND {predicate} AND status IN ('active','uncertain','conflict')",
            (now, now, project_id, *params),
        )
    # A deduplicated fact has one stored source but may be independently present
    # in another surviving accepted commit. Keep that evidence and move its source.
    surviving_facts: dict[str, str] = {}
    for commit in conn.execute(
        "SELECT h.*, c.content FROM chapter_commit_history h JOIN chapters c ON c.id=h.chapter_id AND c.project_id=h.project_id "
        "WHERE h.project_id=? AND h.chapter_id!=? AND h.status='accepted' ORDER BY h.version DESC",
        (project_id, chapter_id),
    ).fetchall():
        if chapter_commits.sha256_hex(commit["content"]) != commit["content_hash"]:
            continue
        for fact in _json(commit["observed_facts_json"], []):
            text = str(fact.get("content") or fact.get("fact") or "").strip() if isinstance(fact, dict) else str(fact).strip()
            if text:
                surviving_facts.setdefault(text, commit["chapter_id"])
    for fact in snapshot["novel_facts"]:
        source = surviving_facts.get(fact["content"])
        if source and fact["status"] in ("active", "uncertain") and not fact["is_conflict"]:
            conn.execute("UPDATE novel_facts SET status='active', valid_to='', chapter_id=?, source_chapter_id=? WHERE id=? AND project_id=?", (source, source, fact["id"], project_id))
    snapshot["chapter_commit_history"] = [dict(row) for row in conn.execute("SELECT * FROM chapter_commit_history WHERE project_id=? AND chapter_id=?", (project_id, chapter_id)).fetchall()]
    conn.execute("UPDATE chapter_commit_history SET status='recycled' WHERE project_id=? AND chapter_id=? AND status='accepted'", (project_id, chapter_id))
    for table in ("chapter_digests", "memory_projections", "chapter_hooks", "story_state_transitions", "chapter_contracts"):
        rows = conn.execute(f"SELECT * FROM {table} WHERE project_id=? AND chapter_id=?", (project_id, chapter_id)).fetchall()
        snapshot[table] = [dict(row) for row in rows]
        if table == "story_state_transitions" and rows:
            _reconcile_state(conn, project_id, dict(rows[0]), snapshot)
        conn.execute(f"DELETE FROM {table} WHERE project_id=? AND chapter_id=?", (project_id, chapter_id))
    memory_fts.delete_chapter_fts(project_id, chapter_id, conn=conn)
    # Explicit volume IDs are exact; old chapters without one make cached volume
    # provenance ambiguous, so invalidate those summaries conservatively.
    if chapter["volume_id"]:
        volumes = conn.execute("SELECT * FROM volumes WHERE project_id=? AND id=?", (project_id, chapter["volume_id"])).fetchall()
    else:
        volumes = conn.execute("SELECT * FROM volumes WHERE project_id=? AND summary!=''", (project_id,)).fetchall()
    snapshot["volumes"] = [dict(row) for row in volumes]
    for volume in volumes:
        conn.execute("UPDATE volumes SET summary='', summary_hash='', updated_at=? WHERE project_id=? AND id=?", (now, project_id, volume["id"]))
    return snapshot
