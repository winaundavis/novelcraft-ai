"""Deterministic Story State seeding and controlled author updates."""

from __future__ import annotations

from typing import Any

from app.dao import foreshadows as foreshadows_dao
from app.dao import projects as projects_dao
from app.dao import story_states_dao


def _first_character(project: dict[str, Any]) -> dict[str, Any]:
    characters = project.get("characters") or []
    return next((item for item in characters if item.get("role") == "protagonist"), characters[0] if characters else {})


def _seed_fields(project: dict[str, Any]) -> dict[str, Any]:
    outline = project.get("outline") or {}
    protagonist = _first_character(project)
    nodes = outline.get("nodes") or []
    first_node = nodes[0] if nodes else {}
    active = foreshadows_dao.list_active_foreshadows(project["id"], limit=20)
    return {
        "current_volume_goal": str(outline.get("volumeGoal") or outline.get("mainSummary") or ""),
        "current_phase_goal": str(outline.get("phaseGoal") or first_node.get("summary") or outline.get("mainSummary") or ""),
        "protagonist_goal": str(protagonist.get("surfaceMotivation") or protagonist.get("deepDesire") or ""),
        "protagonist_obstacle": str(first_node.get("conflict") or ""),
        "main_conflict": str(outline.get("mainConflict") or first_node.get("conflict") or ""),
        "sub_conflicts": list(outline.get("subConflicts") or []),
        "relationship_changes": [],
        "current_mysteries": list(outline.get("mysteries") or []),
        "active_foreshadows": [item.get("name") or item.get("id") for item in active],
        "unresolved_questions": list(outline.get("unresolvedQuestions") or []),
        "promised_payoffs": list(outline.get("promisedPayoffs") or []),
        "last_major_state_change": "",
        "next_stage_directions": list(outline.get("nextDirections") or []),
        "source": "seeded",
    }


def ensure_story_state(project_id: str) -> dict[str, Any]:
    existing = story_states_dao.get_story_state(project_id)
    if existing:
        return existing
    project = projects_dao.get_project(project_id)
    if not project:
        raise ValueError("项目未找到。")
    return story_states_dao.create_story_state(project_id, _seed_fields(project))


def get_story_state(project_id: str) -> dict[str, Any]:
    return ensure_story_state(project_id)


def update_story_state(project_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    ensure_story_state(project_id)
    result = story_states_dao.update_story_state(project_id, fields)
    if not result:
        raise ValueError("剧情状态保存失败。")
    return result
