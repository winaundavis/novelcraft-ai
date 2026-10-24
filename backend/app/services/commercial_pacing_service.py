"""Deterministic commercial pacing compiler for long-form chapter contracts."""

from __future__ import annotations

from typing import Any

CHAPTER_MODES = (
    "Action", "Investigation", "Dialogue", "Conflict", "Reveal",
    "Character", "Emotion", "Breather", "Climax", "Transition",
)
PACING_RENDER_BUDGET = 1800
_MODE_ALIASES = {mode.lower(): mode for mode in CHAPTER_MODES}
_MODE_ALIASES.update({"escalation": "Conflict", "discovery": "Reveal", "setup": "Transition"})


def _text(value: Any, fallback: str = "") -> str:
    return str(value or "").strip()[:600]


def _items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)][:12]


def _nodes(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in (project.get("outline") or {}).get("nodes") or [] if isinstance(node, dict)]


def _mode(node: dict[str, Any], index: int, recent: list[str]) -> str:
    explicit = _text(node.get("chapterMode") or node.get("chapter_mode") or node.get("mode") or node.get("pacingMode"))
    normalized = _MODE_ALIASES.get(explicit.lower())
    if normalized:
        return normalized
    sequence = ("Transition", "Investigation", "Action", "Dialogue", "Conflict", "Reveal", "Character", "Emotion", "Breather", "Climax")
    candidate = sequence[index % len(sequence)]
    if recent and candidate == recent[-1]:
        candidate = sequence[(index + 1) % len(sequence)]
    return candidate


def _arc_for(node: dict[str, Any], index: int, outline: dict[str, Any], key: str) -> dict[str, Any]:
    direct = node.get(key) or node.get(f"{key}Context") or node.get(f"{key}_context")
    if isinstance(direct, dict):
        return {str(k): _text(v) if not isinstance(v, list) else _items(v) for k, v in direct.items()}
    outline_direct = outline.get(key)
    if isinstance(outline_direct, dict):
        return {str(k): _text(v) if not isinstance(v, list) else _items(v) for k, v in outline_direct.items()}
    candidates = outline.get(f"{key}s") or outline.get(f"{key}es") or []
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            start = int(item.get("start", item.get("startChapter", 0)) or 0)
            end = int(item.get("end", item.get("endChapter", 10**9)) or 10**9)
            if start <= index <= end:
                return {str(k): _text(v) if not isinstance(v, list) else _items(v) for k, v in item.items() if k not in {"start", "end", "startChapter", "endChapter"}}
    return {}


def compile_pacing_context(project: dict[str, Any], chapter_id: str, story_state: dict[str, Any] | None = None) -> dict[str, Any]:
    outline = project.get("outline") or {}
    nodes = _nodes(project)
    index = next((i for i, node in enumerate(nodes) if node.get("id") == chapter_id), 0)
    recent_modes = [_mode(node, i, []) for i, node in enumerate(nodes[:index])][-3:]
    node = nodes[index] if nodes else {}
    mysteries = (story_state or {}).get("current_mysteries") or []
    next_directions = (story_state or {}).get("next_stage_directions") or []
    chapter_mode = _mode(node, index, recent_modes)
    chapter_scale = {
        "opening_state": _text(node.get("openingState") or node.get("opening_state") or (story_state or {}).get("last_major_state_change")),
        "goal": _text(node.get("goal") or node.get("summary") or (story_state or {}).get("current_phase_goal"), "推进本章剧情"),
        "conflict": _text(node.get("conflict") or (story_state or {}).get("main_conflict"), "目标受到阻力"),
        "reward_or_change": _text(node.get("reward") or node.get("stateChange") or node.get("revelation"), "产生可感知变化"),
        "new_question": _text(node.get("newQuestion") or node.get("question") or (mysteries[0] if mysteries else "")),
        "hook": _text(node.get("hook") or node.get("endingHook"), "留下下一章阅读理由"),
    }
    small_arc = _arc_for(node, index, outline, "smallArc")
    big_arc = _arc_for(node, index, outline, "bigArc")
    if not big_arc:
        big_arc = {
            "goal": _text(outline.get("volumeGoal") or (story_state or {}).get("current_volume_goal") or outline.get("mainSummary")),
            "climax": _text(outline.get("climax")),
            "next_goal": _text(outline.get("nextGoal") or (next_directions[0] if next_directions else "")),
        }
    return {
        "chapter_id": chapter_id,
        "chapter_index": index,
        "chapter_mode": chapter_mode,
        "recent_modes": recent_modes,
        "chapter_scale": chapter_scale,
        "small_arc": small_arc,
        "big_arc": big_arc,
    }


def render_pacing_context(context: dict[str, Any]) -> str:
    def render(value: Any) -> str:
        if isinstance(value, dict):
            return "；".join(f"{key}={render(item)}" for key, item in value.items() if item not in ("", [], None))
        if isinstance(value, list):
            return "、".join(str(item) for item in value)
        return str(value or "")

    text = "\n".join([
        "【商业节奏合同】",
        f"章节模式：{context.get('chapter_mode', '')}",
        f"章节尺度：{render(context.get('chapter_scale', {}))}",
        f"小 arc：{render(context.get('small_arc', {}))}",
        f"大 arc：{render(context.get('big_arc', {}))}",
        "要求：本章必须产生可感知变化，并为下一章留下具体阅读理由；不要机械重复上一章的模式。",
    ])
    return text[:PACING_RENDER_BUDGET]
