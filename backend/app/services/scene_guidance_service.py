"""Bounded prompt guidance that keeps narrative truth above craft preferences."""

from __future__ import annotations

from typing import Any

from app.utils.world_rules import world_rule_lines


MAX_GUIDANCE_CHARS = 2400


def _values(value: Any, limit: int = 6) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    text = str(value or "").strip()
    return [text] if text else []


def _character_lines(characters: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for character in characters[:10]:
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        basic = character.get("basicInfo") or {}
        identity = str(character.get("identity") or basic.get("identity") or "").strip()
        gender = str(character.get("gender") or basic.get("gender") or "").strip()
        details = [f"身份={identity}" if identity else "", f"性别={gender}" if gender else ""]
        lines.append(f"- {name}" + ("：" + "；".join(item for item in details if item) if any(details) else ""))
    return lines


def build_scene_guidance(
    *,
    characters: list[dict[str, Any]],
    world_rules: list[dict[str, Any]],
    chapter_contract: dict[str, Any] | None,
    story_state: dict[str, Any] | None,
) -> str:
    """Render deterministic prompt hierarchy; never infer missing story facts."""
    contract = chapter_contract or {}
    state = story_state or {}
    lines = [
        "【Hard Story Truth】",
        "以下为已确认事实，优先级高于任何文风、节奏、场景或修辞建议；不得改写、抵消或补充为相反事实。",
        "人物身份与性别：",
        *(_character_lines(characters) or ["- 未提供的人物属性不得补写。"]),
    ]
    if world_rules:
        lines.append("世界规则（硬约束，违反即为设定矛盾）：")
        lines.extend(world_rule_lines(world_rules, max_chars=1200, max_count=24) or ["- 未提供"])
    else:
        lines.append("世界规则：未提供的信息不得补写。")
    for label, key in (("当前目标", "goal"), ("当前障碍", "conflict"), ("关键剧情事实", "state_change")):
        value = str(contract.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    for label, key in (("已确认关系", "relationship_changes"), ("已确认剧情", "last_major_state_change"), ("当前阶段", "current_phase_goal")):
        values = _values(state.get(key))
        if values:
            lines.append(f"{label}：{'；'.join(values)}")
    lines.extend(
        [
            "【Scene Plan / Soft Craft Guidance】",
            "在不破坏 Hard Story Truth、人物一致性和自然叙事的前提下，优先通过动作、环境、感官、人物选择与潜台词增强场景体验。",
            "让本章冲突通过可感知的行动与后果推进；情绪变化应由人物处境和选择带出。",
            "Hard Story Truth 的优先级高于 Soft Craft Guidance。不要新增当前上下文未提供、且会改变世界规则、人物背景或剧情事实的重要设定。",
        ]
    )
    ending_hook = str(contract.get("ending_hook") or "").strip()
    if ending_hook:
        lines.append(f"结尾方向：朝既定钩子或可感知的剧情变化推进（{ending_hook}）。")
    return "\n".join(lines)[:MAX_GUIDANCE_CHARS]
