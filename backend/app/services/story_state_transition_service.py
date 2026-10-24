"""Deterministic post-chapter Story State transition loop (P0-E/Phase B)."""

from __future__ import annotations

from typing import Any

from app.dao import chapters as chapters_dao
from app.dao import foreshadows as foreshadows_dao
from app.dao import projects as projects_dao
from app.dao import story_state_transitions_dao
from app.services import chapter_contract_service, entity_catalog_service, story_state_service


STATE_KEYS = (
    "characters",
    "relationships",
    "knowledge",
    "resources",
    "conflicts",
    "foreshadows",
    "mysteries",
    "next_goals",
    "scene_participants",
)


def _items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:20]
    if value is None:
        return []
    return [str(value).strip()] if str(value).strip() else []


def _split_marked_values(text: str, labels: tuple[str, ...]) -> list[str]:
    """Read optional author/model state markers without attempting semantic inference."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if any(stripped.startswith(f"{label}:") or stripped.startswith(f"{label}：") for label in labels):
            value = stripped.split(":", 1)[-1] if ":" in stripped else stripped.split("：", 1)[-1]
            return _items([part.strip() for part in value.replace("；", ",").split(",")])
    return []


def extract_observed_state(content: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Extract a small, deterministic observed-state schema from finished prose.

    Exact contract terms and explicit state markers are evidence; this function does
    not claim to understand literary meaning or invent facts that are not present.
    """
    text = str(content or "")
    observed: dict[str, Any] = {key: [] for key in STATE_KEYS}
    entity_catalog = ((contract.get("arc_context") or {}).get("entity_catalog") or [])
    characters = _items(contract.get("characters"))
    if entity_catalog:
        observed["characters"] = entity_catalog_service.match_character_mentions(text, entity_catalog)
        observed["scene_participants"] = entity_catalog_service.match_scene_participants(text, entity_catalog)
    else:
        observed["characters"] = [name for name in characters if name and name in text]
        observed["scene_participants"] = _split_marked_values(text, ("场景参与", "场景人物"))
    observed["knowledge"] = list(dict.fromkeys([
        *[item for item in _items(contract.get("key_events")) if item in text],
        *_split_marked_values(text, ("知识获得", "获得知识", "已知信息")),
    ]))
    observed["foreshadows"] = list(dict.fromkeys([
        *[item for item in _items(contract.get("must_payoff")) if item in text],
        *_split_marked_values(text, ("伏笔推进", "伏笔回收", "伏笔埋设")),
    ]))
    observed["conflicts"] = _split_marked_values(text, ("冲突升级", "冲突"))
    observed["relationships"] = _split_marked_values(text, ("关系变化", "关系"))
    observed["resources"] = _split_marked_values(text, ("资源变化", "资源"))
    observed["mysteries"] = _split_marked_values(text, ("新增悬念", "悬念", "问题"))
    observed["next_goals"] = _split_marked_values(text, ("下一目标", "目标"))
    observed["state_changes"] = _split_marked_values(text, ("状态变化", "状态"))
    # Only explicit contract terms/markers are confirmable; inferred semantics stay pending.
    observed["evidence"] = {key: {"status": "observed", "confirmed": bool(values)} for key, values in observed.items() if key != "evidence"}
    observed["pending_confirmation"] = [key for key, values in observed.items() if key not in {"evidence", "pending_confirmation"} and not values]
    return observed


def _planned_state(contract: dict[str, Any]) -> dict[str, Any]:
    state = {
        "characters": _items(contract.get("characters")),
        "relationships": _items(contract.get("relationship_changes")),
        "knowledge": _items(contract.get("key_events")),
        "resources": [],
        "conflicts": _items(contract.get("conflict")),
        "foreshadows": _items(contract.get("must_payoff")),
        "mysteries": _items(contract.get("ending_hook")),
        "next_goals": _items(contract.get("goal")),
        "state_changes": _items(contract.get("state_change")),
    }
    return {key: {"status": "planned", "value": value} for key, value in state.items()}


def detect_state_conflicts(
    planned: dict[str, Any], observed: dict[str, Any], content: str
) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    text = str(content or "")
    # Only mark explicit contradictions. Missing semantic evidence is unverified,
    # not a reason to mutate Story State or to accuse the prose of being wrong.
    negation_terms = ("未发生", "没有", "并未", "失败", "取消")
    for key in ("knowledge", "foreshadows", "state_changes"):
        planned_items = planned.get(key, {}).get("value", []) if isinstance(planned.get(key), dict) else planned.get(key)
        for item in _items(planned_items):
            if item and any(f"{negation}{item}" in text or f"{negation}实现{item}" in text for negation in negation_terms):
                conflicts.append({"type": f"planned_{key}_contradiction", "planned": item, "message": f"计划项被正文明确否定：{item}"})
    if "状态冲突" in text or "设定冲突" in text:
        conflicts.append({"type": "explicit_state_conflict", "planned": "", "message": "正文显式标记需要人工确认的状态冲突。"})
    return conflicts


def _final_state(state: dict[str, Any], observed: dict[str, Any], active: list[dict[str, Any]], next_directions: list[str], state_change: str) -> dict[str, Any]:
    result = dict(state)
    result["last_major_state_change"] = state_change
    result["active_foreshadows"] = [item.get("name") or item.get("id") for item in active]
    previous_relationships = state.get("relationship_changes") or []
    observed_relationships = observed.get("relationships") or []
    result["relationship_changes"] = list(dict.fromkeys([*previous_relationships, *observed_relationships]))[:20]
    previous_mysteries = state.get("current_mysteries") or []
    observed_mysteries = observed.get("mysteries") or []
    result["current_mysteries"] = list(dict.fromkeys([*previous_mysteries, *observed_mysteries]))[:20]
    result["next_stage_directions"] = next_directions
    return result


def _node(project: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    return next((item for item in (project.get("outline") or {}).get("nodes") or [] if item.get("id") == chapter_id), {})


def apply_chapter_transition(
    project_id: str, chapter_id: str, summary: str = "", content: str = "",
    observed_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = story_state_transitions_dao.get_transition(project_id, chapter_id)
    if existing:
        return existing
    project = projects_dao.get_project(project_id)
    chapter = chapters_dao.get_chapter(chapter_id, project_id)
    if not project or not chapter:
        raise ValueError("项目或章节未找到。")
    state = story_state_service.ensure_story_state(project_id)
    contract = chapter_contract_service.get_or_compile_chapter_contract(project_id, chapter_id)
    active = foreshadows_dao.list_active_foreshadows(project_id, limit=20)
    chapters = chapters_dao.list_chapters_global_order(project_id)
    index = next((i for i, item in enumerate(chapters) if item.get("id") == chapter_id), -1)
    next_chapter = chapters[index + 1] if 0 <= index < len(chapters) - 1 else None
    next_node = _node(project, (next_chapter or {}).get("id", "")) if next_chapter else {}
    planned = _planned_state(contract)
    # Formal projections consume immutable commit evidence.  Text extraction is
    # retained only for pre-commit compatibility callers.
    observed = observed_evidence if observed_evidence is not None else extract_observed_state(content or chapter.get("content") or summary, contract)
    conflicts = detect_state_conflicts(planned, observed, content or chapter.get("content") or summary)
    state_change = str(
        (observed.get("state_changes") or [None])[0]
        or contract.get("state_change")
        or summary
        or chapter.get("summary")
        or "本章完成并进入下一阶段。"
    )[:600]
    next_directions = [str(next_node.get("summary") or "").strip()] if next_node.get("summary") else list(state.get("next_stage_directions") or [])
    unresolved = list(state.get("unresolved_questions") or [])
    for question in _items(observed.get("mysteries")):
        if question not in unresolved:
            unresolved.append(question)
    if contract.get("ending_hook") and contract["ending_hook"] not in unresolved:
        unresolved.append(str(contract["ending_hook"]))
    current_final = _final_state(state, observed, active, next_directions, state_change)
    if conflicts:
        return story_state_transitions_dao.save_transition(
            project_id,
            chapter_id,
            {
                "status": "conflict",
                "state_version_before": state.get("version", 1),
                "state_version_after": state.get("version", 1),
                "state_change": state_change,
                "planned_state": planned,
                "observed_state": observed,
                "final_state": current_final,
                "before_state": state,
                "conflicts": conflicts,
                "knowledge_gained": observed.get("knowledge"),
                "losses": observed.get("resources"),
                "relationship_changes": observed.get("relationships"),
                "conflict_escalations": observed.get("conflicts"),
                "foreshadow_updates": observed.get("foreshadows"),
                "unresolved_questions": unresolved,
                "next_directions": next_directions,
            },
        )
    updated = story_state_service.update_story_state(
        project_id,
        {
            "last_major_state_change": current_final["last_major_state_change"],
            "active_foreshadows": current_final["active_foreshadows"],
            "relationship_changes": current_final["relationship_changes"],
            "current_mysteries": current_final["current_mysteries"],
            "unresolved_questions": unresolved,
            "next_stage_directions": next_directions,
        },
    )
    transition = story_state_transitions_dao.save_transition(
        project_id,
        chapter_id,
        {
            "state_version_before": state.get("version", 1),
            "state_version_after": updated.get("version", 1),
            "state_change": state_change,
            "knowledge_gained": observed.get("knowledge"),
            "losses": observed.get("resources"),
            "relationship_changes": observed.get("relationships"),
            "conflict_escalations": observed.get("conflicts"),
            "foreshadow_updates": observed.get("foreshadows"),
            "unresolved_questions": unresolved,
            "next_directions": next_directions,
            "status": "applied",
            "planned_state": planned,
            "observed_state": observed,
            "final_state": updated,
            "before_state": state,
            "conflicts": [],
        },
    )
    return transition


def resolve_state_conflict(project_id: str, chapter_id: str, *, approve: bool) -> dict[str, Any]:
    """Apply or reject a recorded conflict after explicit human confirmation."""
    transition = story_state_transitions_dao.get_transition(project_id, chapter_id)
    if not transition or transition.get("status") != "conflict":
        raise ValueError("未找到待确认的状态冲突。")
    if not approve:
        return story_state_transitions_dao.update_decision(
            project_id, chapter_id, status="rejected", final_state=transition.get("final_state") or {}
        ) or transition
    candidate = transition.get("final_state") or {}
    allowed = {
        key: candidate.get(key)
        for key in (
            "current_volume_goal", "current_phase_goal", "protagonist_goal", "protagonist_obstacle",
            "main_conflict", "sub_conflicts", "relationship_changes", "current_mysteries",
            "active_foreshadows", "unresolved_questions", "promised_payoffs", "last_major_state_change",
            "next_stage_directions",
        )
        if key in candidate
    }
    updated = story_state_service.update_story_state(project_id, allowed)
    return story_state_transitions_dao.update_decision(
        project_id, chapter_id, status="applied", final_state=updated
    ) or transition
