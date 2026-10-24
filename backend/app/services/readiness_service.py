"""Zero-API long-form readiness metrics derived from persisted project data."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.dao import chapter_contracts_dao, chapters as chapters_dao, foreshadows as foreshadows_dao
from app.dao import story_state_transitions_dao, chapter_commits
from app.db import get_conn
from app.services import chapter_change_service
from app.services import doctor_service
from app.llm.audit import query_llm_audit


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def build_readiness(project: dict[str, Any], project_id: str) -> dict[str, Any]:
    chapters = chapters_dao.list_chapters_global_order(project_id)
    completed = [item for item in chapters if item.get("status") == "completed" or item.get("content")]
    contracts = chapter_contracts_dao.list_latest_contracts(project_id)
    transitions = story_state_transitions_dao.list_transitions(project_id, limit=10000)
    canonical_commits = chapter_commits.list_canonical_commits(project_id)
    with get_conn() as conn:
        has_digest_table = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chapter_digests'").fetchone())
        digest_count = int(conn.execute("SELECT COUNT(*) AS n FROM chapter_digests WHERE project_id = ?", (project_id,)).fetchone()["n"]) if has_digest_table else 0
        projection_debt = int(conn.execute("SELECT COUNT(*) AS n FROM memory_projections WHERE project_id = ? AND status IN ('failed', 'pending')", (project_id,)).fetchone()["n"])
    foreshadows = foreshadows_dao.list_foreshadows(project_id)
    change_report = chapter_change_service.build_change_report(chapters, transitions, contracts)
    modes = [item.get("chapter_mode") for item in contracts if item.get("chapter_mode")]
    repeated_modes = sum(a == b for a, b in zip(modes, modes[1:]))
    hooks = sum(bool(item.get("summary")) for item in completed)
    active_foreshadows = sum(item.get("status") not in {"resolved", "completed", "closed"} for item in foreshadows)
    audit_rows = query_llm_audit(project_id=project_id, limit=10000)
    calls = len(audit_rows)
    total_tokens = sum(int(item.get("total_tokens") or 0) for item in audit_rows)
    retry_calls = sum(1 for item in audit_rows if "retry" in str(item.get("operation") or ""))
    rewrite_calls = sum(1 for item in audit_rows if "rewrite" in str(item.get("operation") or ""))
    style_config = project.get("style_config") or {}
    if isinstance(style_config, str):
        style_config = {}
    profile_present = bool(style_config.get("profile") or style_config.get("preset"))
    genre_present = bool(project.get("genre"))
    return {
        "project_id": project_id,
        "api_calls": 0,
        "data_basis": {"chapters": len(chapters), "completed_chapters": len(completed), "contracts": len(contracts), "transitions": len(transitions), "canonical_commits": len(canonical_commits), "digests": digest_count},
        "continuity": {"status": "measured" if completed else "insufficient_data", "metrics": {"chapter_state_coverage": _ratio(len(transitions), len(completed)), "active_foreshadows": active_foreshadows, "repetition_possible_issue": change_report["repetition"]["possible_issue"]}},
        "narrative": {"status": "measured" if completed else "insufficient_data", "metrics": {"state_transition_rate": _ratio(len(transitions), len(completed)), "contract_coverage": _ratio(len(contracts), len(chapters)), "repeated_mode_rate": _ratio(repeated_modes, max(len(modes) - 1, 0)), **change_report["metrics"]}},
        "style": {"status": "signals_only", "metrics": {"preset_deviation_rate": None, "style_profile_coverage": _ratio(sum(bool(c.get("style_profile")) for c in contracts), len(contracts)) if contracts else None, "genre_profile_presence": genre_present, "label": "Style Compliance Signals"}},
        "commercial": {"status": "measured" if completed else "insufficient_data", "metrics": {"completed_chapters": len(completed), "chapter_hook_coverage": _ratio(hooks, len(completed)), "mode_count": len(set(modes)), "active_foreshadows": active_foreshadows}},
        "cost": {"status": "measured" if calls else "not_measured", "metrics": {"calls_per_chapter": _ratio(calls, len(completed)) if completed else None, "tokens_per_chapter": _ratio(total_tokens, len(completed)) if completed and total_tokens else None, "retry_rate": _ratio(retry_calls, calls) if calls else None, "rewrite_rate": _ratio(rewrite_calls, calls) if calls else None, "actual_tokens": total_tokens if calls else None, "projection_api_calls": 0}},
        "projection": {"status": "healthy" if projection_debt == 0 else "debt", "metrics": {"canonical_commits": len(canonical_commits), "digest_coverage": _ratio(digest_count, len(canonical_commits)), "failed_or_pending": projection_debt}},
        "chapter_change_report": change_report,
    }


def experiment_readiness(project_id: str, *, core_tests_pass: bool, default_suite_zero_api: bool) -> dict[str, Any]:
    """Read-only admission gate. Cost stays unknown until a user supplies a plan."""
    doctor = doctor_service.inspect_project(project_id)
    blocking_reasons: list[str] = []
    if not core_tests_pass:
        blocking_reasons.append("core_tests_failed")
    if not default_suite_zero_api:
        blocking_reasons.append("default_suite_not_zero_api")
    if doctor["status"] == "FAIL":
        blocking_reasons.append("canonical_or_projection_health_failed")
    # Prices and exact call plans are external facts. Never infer them.
    blocking_reasons.append("cost_plan_unknown")
    return {
        "status": "READY" if not blocking_reasons else "BLOCKED",
        "project_id": project_id,
        "cost_estimate": "unknown",
        "core_tests_pass": core_tests_pass,
        "default_suite_zero_api": default_suite_zero_api,
        "doctor_status": doctor["status"],
        "blocking_reasons": blocking_reasons,
    }
