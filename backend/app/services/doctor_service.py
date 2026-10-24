"""Read-only health inspection for one NovelCraft project."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.dao import chapter_commits
from app.db import DB_PATH, get_conn

CANONICAL_PROJECTIONS = {"story_state", "memory", "summary", "index", "foreshadow"}


def _finding(level: str, area: str, reason: str, suggestion: str, **context: Any) -> dict[str, Any]:
    return {"level": level, "area": area, "reason": reason, "action": suggestion, "suggestion": suggestion, **context}


def inspect_project(project_id: str) -> dict[str, Any]:
    """Return explainable PASS/WARN/FAIL findings without mutating DB or files."""
    findings: list[dict[str, Any]] = []
    if not Path(DB_PATH).exists():
        findings.append(_finding("FAIL", "project", "SQLite database is missing", "initialize the project database"))
        return {"status": "FAIL", "project_id": project_id, "findings": findings}
    with get_conn() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"projects", "chapters", "chapter_commit_history", "memory_projections", "chapter_digests", "story_states"}
        for table in sorted(required - tables):
            findings.append(_finding("FAIL", "schema", f"required table {table} is missing", "run database initialization", table=table))
        project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            findings.append(_finding("FAIL", "project", "project does not exist", "select an existing project", project_id=project_id))
        else:
            chapters = conn.execute("SELECT id, chapter_order FROM chapters WHERE project_id = ? ORDER BY chapter_order, created_at", (project_id,)).fetchall()
            commit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chapter_commit_history)").fetchall()}
            orders = [row["chapter_order"] for row in chapters]
            if len(orders) != len(set(orders)):
                findings.append(_finding("WARN", "story", "duplicate chapter order values", "verify ordering before a long run"))
            for row in chapters:
                if "status" in commit_columns:
                    accepted_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM chapter_commit_history "
                        "WHERE project_id = ? AND chapter_id = ? AND COALESCE(status, 'accepted') = 'accepted'",
                        (project_id, row["id"]),
                    ).fetchone()["n"]
                    if accepted_count > 1:
                        findings.append(_finding(
                            "FAIL", "commit", "chapter has multiple canonical accepted commits",
                            "manual_review_required: reconcile commit statuses before replay",
                            chapter_id=row["id"], accepted_count=accepted_count, manual_review_required=True,
                        ))
                matched = chapter_commits.content_matches_commit(project_id, row["id"])
                if matched is None:
                    findings.append(_finding("WARN", "commit", "chapter has no accepted commit", "create an accepted chapter commit", chapter_id=row["id"]))
                elif not matched:
                    findings.append(_finding("FAIL", "commit", "current chapter content differs from accepted commit", "create a new chapter version and commit", chapter_id=row["id"]))
                elif chapter_commits.get_canonical_commit(project_id, row["id"]) is None:
                    findings.append(_finding("FAIL", "commit", "chapter has no canonical accepted commit", "restore or create an accepted commit", chapter_id=row["id"]))
                else:
                    digest = conn.execute("SELECT commit_id FROM chapter_digests WHERE project_id = ? AND chapter_id = ?", (project_id, row["id"])).fetchone()
                    canonical = chapter_commits.get_canonical_commit(project_id, row["id"])
                    if not digest:
                        findings.append(_finding("WARN", "digest", "chapter has no deterministic digest projection", "replay summary projection", chapter_id=row["id"]))
                    elif canonical and digest["commit_id"] != canonical["id"]:
                        findings.append(_finding("FAIL", "digest", "digest belongs to superseded commit", "replay summary projection", chapter_id=row["id"], manual_review_required=False))
            projection_rows = conn.execute("SELECT chapter_id, projection_type, status, detail, commit_id FROM memory_projections WHERE project_id = ?", (project_id,)).fetchall()
            for row in projection_rows:
                if row["projection_type"] not in CANONICAL_PROJECTIONS:
                    continue  # legacy adapter state is intentionally non-authoritative
                canonical = chapter_commits.get_canonical_commit(project_id, row["chapter_id"])
                if canonical and row["commit_id"] and row["commit_id"] != canonical["id"]:
                    findings.append(_finding("FAIL", "projection", "projection belongs to a non-canonical commit", "retry canonical projection", chapter_id=row["chapter_id"], projection=row["projection_type"], manual_review_required=False))
                    continue
                if canonical and not row["commit_id"]:
                    findings.append(_finding(
                        "WARN", "projection", "projection has no canonical commit provenance",
                        "retry canonical projection to bind its commit provenance",
                        chapter_id=row["chapter_id"], projection=row["projection_type"], manual_review_required=False,
                    ))
                if row["status"] in {"failed", "pending"}:
                    level = "FAIL" if row["status"] == "failed" else "WARN"
                    findings.append(_finding(level, "projection", f"projection status is {row['status']}: {row['detail'] or 'no detail'}", "retry projection for this chapter", chapter_id=row["chapter_id"], projection=row["projection_type"], status=row["status"]))
            state = conn.execute("SELECT version FROM story_states WHERE project_id = ?", (project_id,)).fetchone()
            if state is None:
                findings.append(_finding("WARN", "memory", "story state is unavailable", "seed or restore story state"))
            elif int(state["version"] or 0) < 1:
                findings.append(_finding(
                    "FAIL", "story_state", "story state version is invalid",
                    "manual_review_required: restore a valid story-state snapshot",
                    version=state["version"], manual_review_required=True,
                ))
            else:
                transition = conn.execute(
                    "SELECT MAX(state_version_after) AS max_version FROM story_state_transitions WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                max_transition_version = int(transition["max_version"] or 0) if transition else 0
                if max_transition_version and int(state["version"] or 0) < max_transition_version:
                    findings.append(_finding(
                        "FAIL", "story_state", "story state version is behind its recorded transitions",
                        "manual_review_required: rebuild story state from canonical commits",
                        version=state["version"], transition_version=max_transition_version, manual_review_required=True,
                    ))
            if "chapter_fts" not in tables:
                findings.append(_finding("WARN", "index", "FTS index is unavailable", "use the built-in LIKE fallback or enable SQLite FTS5"))
    if not findings:
        findings.append(_finding("PASS", "project", "all inspected commit, projection, memory, and index checks passed", "no action required"))
    status = "FAIL" if any(item["level"] == "FAIL" for item in findings) else "WARN" if any(item["level"] == "WARN" for item in findings) else "PASS"
    return {"status": status, "project_id": project_id, "findings": findings}
