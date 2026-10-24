"""Zero-API descriptive pacing, emotion and style-compliance signals."""

from __future__ import annotations

from typing import Any

from app.services import entity_catalog_service


def character_consistency_signal(chapters: list[dict[str, Any]], contracts: list[dict[str, Any]]) -> dict[str, float | int]:
    """Measure identity evidence, separating name mention from scene presence."""
    contract_map = {
        str(row.get("chapter_id") or row.get("chapterId") or ""): row
        for row in contracts or []
    }
    expected = mentioned = present = 0
    for chapter in chapters or []:
        chapter_id = str(chapter.get("id") or chapter.get("chapter_id") or "")
        contract = contract_map.get(chapter_id, {})
        entity_ids = [str(item) for item in contract.get("characters") or []]
        expected += len(entity_ids)
        catalog = ((contract.get("arc_context") or {}).get("entity_catalog") or [])
        text = str(chapter.get("content") or "")
        if catalog:
            mentioned_ids = entity_catalog_service.match_character_mentions(text, catalog)
            present_ids = entity_catalog_service.match_scene_participants(text, catalog)
        else:
            mentioned_ids = [entity_id for entity_id in entity_ids if entity_id in text]
            present_ids = []
        mentioned += sum(entity_id in mentioned_ids for entity_id in entity_ids)
        present += sum(entity_id in present_ids for entity_id in entity_ids)
    return {
        "expected": expected,
        "mentioned": mentioned,
        "mention_coverage": round(mentioned / expected, 4) if expected else 0.0,
        "present": present,
        "presence_coverage": round(present / expected, 4) if expected else 0.0,
    }


def foreshadow_lifecycle(foreshadows: list[dict[str, Any]]) -> dict[str, int]:
    """Count lifecycle events; one final status alone is not a payoff signal."""
    result = {"introduced": 0, "progressed": 0, "paid_off": 0, "unverified": 0}
    for foreshadow in foreshadows or []:
        events = foreshadow.get("events") or []
        statuses = {str(event.get("status") or "") for event in events if isinstance(event, dict)}
        if not events:
            result["unverified"] += 1
            continue
        if "planted" in statuses:
            result["introduced"] += 1
        if "progressing" in statuses:
            result["progressed"] += 1
        if "resolved" in statuses:
            result["paid_off"] += 1
    return result


def emotion_timeline(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous = ""
    same = 0
    result = []
    for item in contracts:
        emotion = str(item.get("emotion") or "")
        same = same + 1 if emotion and emotion == previous else 1 if emotion else 0
        result.append({"chapter_id": item.get("chapter_id") or item.get("chapterId"), "chapter_emotion": emotion or None, "previous_emotion": previous or None, "emotion_delta": bool(emotion and previous and emotion != previous), "consecutive_same_emotion": same})
        previous = emotion or previous
    return result


def pacing_timeline(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"chapter_id": c.get("chapter_id") or c.get("chapterId"), "conflict": bool(c.get("conflict")), "progression": bool(c.get("state_change") or c.get("goal")), "reveal": bool(c.get("revelation") or c.get("key_events")), "payoff": bool(c.get("must_payoff")), "hook": bool(c.get("ending_hook")), "breather": str(c.get("chapter_mode") or "").lower() in {"daily", "slice_of_life"}, "climax": str(c.get("chapter_mode") or "").lower() == "climax"} for c in contracts]


def classify_style_constraints(contract: dict[str, Any]) -> dict[str, list[str]]:
    return {"hard": [str(x) for x in contract.get("hard_constraints", [])], "soft": [str(x) for x in contract.get("soft_constraints", [])], "negative": [str(x) for x in contract.get("negative_constraints", [])]}


def build_long_form_signals(chapters: list[dict[str, Any]], transitions: list[dict[str, Any]], contracts: list[dict[str, Any]], foreshadows: list[dict[str, Any]]) -> dict[str, float | int]:
    """Objective Content Signals only; no literary-quality conclusion is implied."""
    total = len(chapters)
    modes = [str(c.get("chapter_mode") or "") for c in contracts]
    emotion_rows = emotion_timeline(contracts)
    lifecycle = foreshadow_lifecycle(foreshadows)
    return {
        "chapter_change_density": round(len(transitions) / total, 4) if total else 0.0,
        "state_conflict_rate": round(sum(t.get("status") == "conflict" for t in transitions) / max(len(transitions), 1), 4),
        "relationship_change_rate": round(sum(bool(t.get("relationship_changes")) for t in transitions) / total, 4) if total else 0.0,
        "knowledge_change_rate": round(sum(bool(t.get("knowledge_gained")) for t in transitions) / total, 4) if total else 0.0,
        "foreshadow_progress_rate": round((lifecycle["progressed"] + lifecycle["paid_off"]) / max(len(foreshadows), 1), 4),
        "foreshadow_lifecycle": lifecycle,
        "payoff_rate": round(sum(bool(c.get("must_payoff")) for c in contracts) / total, 4) if total else 0.0,
        "chapter_mode_repeat_rate": round(sum(a == b for a, b in zip(modes, modes[1:])) / max(len(modes) - 1, 1), 4) if len(modes) > 1 else 0.0,
        "structural_repeat_rate": round(sum(a == b for a, b in zip(modes, modes[1:])) / max(len(modes) - 1, 1), 4) if len(modes) > 1 else 0.0,
        "emotion_repeat_run": max((int(row["consecutive_same_emotion"]) for row in emotion_rows), default=0),
        "style_profile_coverage": round(sum(bool(c.get("style_profile")) for c in contracts) / total, 4) if total else 0.0,
        "character_voice_coverage": round(sum(bool(c.get("characters")) for c in contracts) / total, 4) if total else 0.0,
    }
