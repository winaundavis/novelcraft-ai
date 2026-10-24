"""Small deterministic pre-commit gate.  It has no model side effects."""

from __future__ import annotations

from typing import Any

from app.dao import chapter_contracts_dao, novel_facts, story_states_dao
from app.services.chapter_budget_service import ChapterGenerationBudget


def evaluate_precommit(project_id: str, chapter_id: str, content: str, *, expected_version: int, budget: ChapterGenerationBudget | None = None, qa_completed: bool = True, contract_override: dict[str, Any] | None = None, story_state_available: bool | None = None, conflict_count: int | None = None) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    if not (content or "").strip():
        blockers.append({"code": "content_missing", "reason": "chapter content is empty"})
    contract = contract_override if contract_override is not None else chapter_contracts_dao.get_latest_contract(project_id, chapter_id)
    if contract is None:
        blockers.append({"code": "contract_missing", "reason": "chapter contract is required"})
    elif int(contract.get("version") or 0) != int(expected_version):
        blockers.append({"code": "contract_version", "reason": "chapter contract version changed"})
    state_is_available = story_state_available if story_state_available is not None else story_states_dao.get_story_state(project_id) is not None
    if not state_is_available:
        blockers.append({"code": "story_state_missing", "reason": "story state is not readable"})
    if not qa_completed:
        blockers.append({"code": "qa_incomplete", "reason": "required QA is incomplete"})
    if (conflict_count if conflict_count is not None else len(novel_facts.list_writing_conflicts(project_id))) > 0:
        blockers.append({"code": "blocking_conflict", "reason": "unresolved story conflict exists"})
    if budget is not None and any(
        getattr(budget, f"{dimension}_calls") > getattr(budget, f"max_{dimension}_calls")
        for dimension in ("generation", "retry", "qa", "rewrite")
    ):
        blockers.append({"code": "budget_exceeded", "reason": "chapter call budget is exceeded"})
    return {"allowed": not blockers, "blockers": blockers, "contract": contract}
