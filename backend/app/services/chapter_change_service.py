"""Deterministic chapter-change and repetition measurements.

This module reports evidence only. It never changes Story State, chapter modes,
or generation behavior.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

CHANGE_FIELDS = {
    "state_change_count": "state_changes",
    "relationship_change_count": "relationships",
    "knowledge_change_count": "knowledge",
    "conflict_change_count": "conflicts",
    "new_question_count": "mysteries",
    "payoff_count": "foreshadows",
}
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def _items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _observed(transition: dict[str, Any] | None) -> dict[str, Any]:
    if not transition:
        return {}
    value = transition.get("observed_state") or transition.get("observedState") or {}
    return value if isinstance(value, dict) else {}


def measure_change(transition: dict[str, Any] | None, previous_transition: dict[str, Any] | None = None) -> dict[str, Any]:
    """Count explicit observed state evidence and deltas from the prior state."""
    observed = _observed(transition)
    previous = _observed(previous_transition)
    metrics: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for metric, field in CHANGE_FIELDS.items():
        current = _items(observed.get(field))
        before = set(_items(previous.get(field)))
        values = [item for item in current if item not in before] if previous_transition else current
        metrics[metric] = len(values)
        evidence[field] = values
    total = sum(metrics.values())
    return {"metrics": metrics, "evidence": evidence, "total_change_count": total, "has_evidence": total > 0}


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_RE.findall(text or "") if len(item.strip()) >= 8]


def _paragraph_shape(text: str) -> tuple[int, ...]:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text or "") if item.strip()]
    return tuple(min(len(item) // 80, 12) for item in paragraphs[:16])


def detect_repetition(
    chapters: list[dict[str, Any]],
    contracts: list[dict[str, Any]] | None = None,
    *,
    recent_window: int = 5,
) -> dict[str, Any]:
    """Return evidence for repeated sentences, paragraph shapes, and chapter modes."""
    ordered = list(chapters or [])
    recent = ordered[-max(recent_window, 1):]
    sentence_counter: Counter[str] = Counter()
    repeated_sentences: list[dict[str, Any]] = []
    for chapter in recent:
        sentences = _sentences(str(chapter.get("content") or ""))
        for sentence in sentences:
            if sentence in sentence_counter and not any(item["text"] == sentence for item in repeated_sentences):
                repeated_sentences.append({"text": sentence, "count": sentence_counter[sentence] + 1})
            sentence_counter[sentence] += 1

    shapes = [_paragraph_shape(str(chapter.get("content") or "")) for chapter in recent]
    repeated_shapes = sum(bool(shape) and shape == previous for shape, previous in zip(shapes, shapes[1:]))
    mode_by_chapter = {str(item.get("chapter_id") or item.get("chapterId") or ""): str(item.get("chapter_mode") or "") for item in contracts or []}
    modes = [mode_by_chapter.get(str(chapter.get("id") or chapter.get("chapter_id") or ""), str(chapter.get("chapter_mode") or "")) for chapter in recent]
    modes = [mode for mode in modes if mode]
    mode_run = 0
    if modes:
        for mode in reversed(modes):
            if mode == modes[-1]:
                mode_run += 1
            else:
                break
    reasons: list[str] = []
    if repeated_sentences:
        reasons.append("repeated_sentences")
    if repeated_shapes >= 2:
        reasons.append("repeated_paragraph_shape")
    if mode_run >= 3:
        reasons.append("repeated_chapter_mode")
    return {
        "window": len(recent),
        "repeated_sentences": repeated_sentences,
        "repeated_paragraph_shapes": repeated_shapes,
        "recent_modes": modes,
        "mode_run": mode_run,
        "possible_issue": bool(reasons),
        "reasons": reasons,
    }


def build_change_report(
    chapters: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    contracts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a zero-API project report and a conservative stop recommendation."""
    ordered = list(chapters or [])
    transition_map = {str(item.get("chapter_id") or item.get("chapterId") or ""): item for item in transitions or []}
    contract_map = {str(item.get("chapter_id") or item.get("chapterId") or ""): item for item in contracts or []}
    chapter_changes: list[dict[str, Any]] = []
    previous_transition = None
    no_change_streak = 0
    max_no_change_streak = 0
    for chapter in ordered:
        chapter_id = str(chapter.get("id") or chapter.get("chapter_id") or "")
        transition = transition_map.get(chapter_id)
        change = measure_change(transition, previous_transition)
        item = {"chapter_id": chapter_id, **change}
        chapter_changes.append(item)
        if change["has_evidence"]:
            no_change_streak = 0
        else:
            no_change_streak += 1
            max_no_change_streak = max(max_no_change_streak, no_change_streak)
        if transition:
            previous_transition = transition
    repetition = detect_repetition(ordered, list(contract_map.values()))
    reasons = list(repetition["reasons"])
    if max_no_change_streak >= 3:
        reasons.append("consecutive_chapters_without_observed_change")
    stop_recommended = bool(reasons)
    completed = len(ordered)
    changed = sum(1 for item in chapter_changes if item["has_evidence"])
    return {
        "api_calls": 0,
        "chapter_changes": chapter_changes,
        "repetition": repetition,
        "metrics": {
            "chapter_change_rate": round(changed / completed, 4) if completed else 0.0,
            "max_no_change_streak": max_no_change_streak,
            "chapters_with_observed_change": changed,
            "chapters_measured": completed,
        },
        "stop_recommended": stop_recommended,
        "stop_reasons": reasons,
        "is_literary_judgment": False,
    }
