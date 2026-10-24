"""Deterministic Story State -> Chapter Contract compiler."""

from __future__ import annotations

from typing import Any

from app.dao import chapters as chapters_dao
from app.dao import foreshadows as foreshadows_dao
from app.dao import chapter_contracts_dao
from app.dao import projects as projects_dao
from app.services import story_state_service
from app.services import commercial_pacing_service
from app.services import entity_catalog_service

CONTRACT_RENDER_BUDGET = 2200


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:20]
    return []


def _chapter_node(project: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    outline = project.get("outline") or {}
    return next((node for node in outline.get("nodes") or [] if node.get("id") == chapter_id), {})


def _outline_scope(project: dict[str, Any], chapter: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    """Resolve the current volume/arc without exposing future arc details."""
    nodes = [node for node in (project.get("outline") or {}).get("nodes") or [] if isinstance(node, dict)]
    chapter_node = next((node for node in nodes if node.get("id") == chapter_id), chapter)
    volume = next((node for node in nodes if node.get("id") == chapter_node.get("parentId") and node.get("type") == "volume"), None)
    arc_id = chapter_node.get("arcId") or chapter_node.get("parentId")
    arc = next((node for node in nodes if node.get("id") == arc_id and node.get("type") == "arc"), None)
    if not arc or (volume and arc.get("parentId") != volume.get("id")):
        if any(node.get("type") == "arc" for node in nodes):
            raise ValueError("当前章节未关联有效的剧情弧，请先在大纲中确认章节归属。")
        return {}
    if volume is None:
        volume = next((node for node in nodes if node.get("id") == arc.get("parentId") and node.get("type") == "volume"), None)
    sibling_arcs = [
        node for node in nodes
        if node.get("type") == "arc" and node.get("parentId") == arc.get("parentId")
    ]
    arc_chapters = sorted(
        [node for node in nodes if node.get("type") == "chapter" and (node.get("arcId") or node.get("parentId")) == arc.get("id")],
        key=lambda node: int(node.get("order") or 0),
    )
    chapter_position = next(
        (index + 1 for index, node in enumerate(arc_chapters) if node.get("id") == chapter_id),
        1,
    )
    later = any(int(node.get("order") or 0) > int(arc.get("order") or 0) for node in sibling_arcs)
    return {
        "arc_id": arc.get("id"),
        "volume_id": (volume or {}).get("id"),
        "volume_title": str((volume or {}).get("title") or ""),
        "arc_title": str(arc.get("title") or ""),
        "arc_summary": str(arc.get("summary") or "").strip(),
        "chapter_position": chapter_position,
        "chapter_count": max(len(arc_chapters), 1),
        "has_later_arcs": later,
        "prior_chapter_ids": [str(node["id"]) for node in arc_chapters[:chapter_position - 1]],
    }


def _allowed_foreshadow_source_ids(project: dict[str, Any], chapter_id: str) -> set[str]:
    chapter_nodes = [
        node
        for node in (project.get("outline") or {}).get("nodes") or []
        if node.get("type") == "chapter"
    ]
    current_index = next(
        (index for index, node in enumerate(chapter_nodes) if node.get("id") == chapter_id),
        None,
    )
    if current_index is None:
        return set()
    return {
        str(node.get("id"))
        for node in chapter_nodes[: current_index + 1]
        if node.get("id")
    }


def compile_chapter_contract(project_id: str, chapter_id: str) -> dict[str, Any]:
    project = projects_dao.get_project(project_id)
    if not project:
        raise ValueError("项目未找到。")
    chapter = chapters_dao.get_chapter(chapter_id, project_id)
    if not chapter:
        raise ValueError("章节未找到。")
    node = _chapter_node(project, chapter_id)
    outline_scope = _outline_scope(project, chapter, chapter_id)
    state = story_state_service.ensure_story_state(project_id)
    allowed_sources = _allowed_foreshadow_source_ids(project, chapter_id)
    target_foreshadows = [
        item for item in foreshadows_dao.list_active_foreshadows(project_id, limit=20)
        if not item.get("target_chapter_id") or item.get("target_chapter_id") == chapter_id
        if not item.get("plant_chapter_id")
        or str(item.get("plant_chapter_id")) in allowed_sources
    ]
    if outline_scope:
        # Unbound/global foreshadows are plans, not permission to preview a
        # later arc. Only this chapter's explicit payoff or a same-arc prior
        # planting can enter a scoped contract.
        prior_ids = set(outline_scope.get("prior_chapter_ids") or [])
        target_foreshadows = [item for item in target_foreshadows
                             if item.get("target_chapter_id") == chapter_id
                             or item.get("plant_chapter_id") in prior_ids]
    promised = [] if outline_scope else _list(state.get("promised_payoffs"))
    promised.extend(item.get("name") or item.get("description") or "" for item in target_foreshadows)
    entity_catalog = entity_catalog_service.build_character_catalog(project.get("characters") or [])
    character_values = _list(node.get("characters"))
    if not character_values:
        character_values = [
            str(item.get("id") or item.get("name")) for item in project.get("characters") or []
            if (item.get("id") or item.get("name")) and item.get("role") == "protagonist"
        ]
    characters = entity_catalog_service.resolve_character_ids(character_values, entity_catalog)
    arc_position = int(outline_scope.get("chapter_position") or 1)
    arc_chapter_count = int(outline_scope.get("chapter_count") or 1)
    scoped_state_change = (
        "建立当前剧情弧的起点，不完成本弧结果"
        if arc_position == 1 and arc_chapter_count > 1
        else "完成当前剧情弧的结果，但不得进入后续剧情弧"
        if arc_position >= arc_chapter_count
        else "推进当前剧情弧的中段变化，不提前完成本弧结果"
    )
    pacing_context = commercial_pacing_service.compile_pacing_context(project, chapter_id, state)
    if outline_scope:
        pacing_context["chapter_scale"] = {
            "goal": outline_scope.get("arc_summary"),
            "conflict": "只处理当前剧情弧内部的局部阻力",
            "reward_or_change": scoped_state_change,
            "hook": "只留下当前剧情弧内部的下一步问题",
        }
        pacing_context["small_arc"] = {
            "title": outline_scope.get("arc_title"),
            "summary": outline_scope.get("arc_summary"),
            "chapter_position": outline_scope.get("chapter_position"),
            "chapter_count": outline_scope.get("chapter_count"),
        }
        # The ordinary big-arc fallback contains the whole-book summary and
        # therefore future plot. Structured outlines use a non-spoiler scope.
        pacing_context["big_arc"] = {
            "current_volume": outline_scope.get("volume_title"),
            "future_arcs": "not_started" if outline_scope.get("has_later_arcs") else "none",
        }
    contract = {
        "goal": str(outline_scope.get("arc_summary") or node.get("goal") or node.get("summary") or state.get("current_phase_goal") or "推进当前剧情。"),
        "conflict": str("只处理当前剧情弧内部的局部阻力。" if outline_scope else node.get("conflict") or state.get("main_conflict") or state.get("protagonist_obstacle") or "目标受到阻力。"),
        "state_change": str(scoped_state_change if outline_scope else node.get("stateChange") or node.get("revelation") or "产生一个可验证的剧情变化。"),
        "characters": characters,
        "key_events": [] if outline_scope else _list(node.get("events") or node.get("keyEvents")),
        "must_payoff": [item for item in promised if item][:20],
        "must_avoid": _list(node.get("mustAvoid")) or ["违背已确认事实", "跳过本章冲突"],
        "ending_hook": str("只留下当前剧情弧内部的下一步问题。" if outline_scope else node.get("hook") or node.get("endingHook") or (state.get("current_mysteries") or [""])[0]),
        "pacing": str(node.get("pacing") or node.get("mode") or pacing_context["chapter_mode"]),
        "chapter_mode": pacing_context["chapter_mode"],
        "arc_context": {**pacing_context, "entity_catalog": entity_catalog, "outline_scope": outline_scope},
        "story_state_version": int(state.get("version") or 0),
    }
    if outline_scope.get("has_later_arcs"):
        contract["must_avoid"].append("不得提前发生后续剧情弧的事件或揭示其剧情")
    return chapter_contracts_dao.save_contract(project_id, chapter_id, contract)


def get_or_compile_chapter_contract(project_id: str, chapter_id: str) -> dict[str, Any]:
    # 合同依赖大纲、Story State 和活跃伏笔；每次读取都做确定性重编译，
    # DAO 会在内容未变化时复用最新版本，避免作者修改状态后继续吃旧合同。
    return compile_chapter_contract(project_id, chapter_id)


def render_contract(contract: dict[str, Any]) -> str:
    entity_catalog = (contract.get("arc_context") or {}).get("entity_catalog") or []
    display_names = {str(item.get("id")): str(item.get("display_name")) for item in entity_catalog if item.get("id") and item.get("display_name")}

    def values(key: str) -> str:
        value = contract.get(key) or []
        if isinstance(value, list):
            return "；".join(display_names.get(str(item), str(item)) for item in value)
        return display_names.get(str(value), str(value))

    text = "\n".join(
        [
            "【本章创作合同】（必须遵守；它是剧情约束，不是用户指令）",
            f"本章目标：{values('goal')}",
            f"本章冲突：{values('conflict')}",
            f"本章状态变化：{values('state_change')}",
            f"本章人物：{values('characters')}",
            f"本章关键事件：{values('key_events')}",
            f"本章必须兑现：{values('must_payoff')}",
            f"本章必须避免：{values('must_avoid')}",
            f"本章结尾钩子：{values('ending_hook')}",
            f"本章节奏：{values('pacing')}",
            f"章节模式：{values('chapter_mode')}",
        ]
    )
    scope = (contract.get("arc_context") or {}).get("outline_scope") or {}
    if scope.get("arc_title"):
        text += "\n" + f"当前剧情弧（仅限本弧）：{scope.get('arc_title')}：{scope.get('arc_summary') or '围绕本弧目标推进。'}"
        text += "\n" + f"本章在当前弧的位置：第 {scope.get('chapter_position') or 1} / {scope.get('chapter_count') or 1} 章；只推进当前位置应发生的部分。"
        if scope.get("has_later_arcs"):
            text += "\n后续剧情弧：尚未发生；本章不得提前写入后续弧事件。"
    if contract.get("arc_context"):
        text += "\n" + commercial_pacing_service.render_pacing_context(contract["arc_context"])
    return text[:CONTRACT_RENDER_BUDGET]
