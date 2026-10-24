"""Deterministic Regression Benchmark, not a literary-quality benchmark."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def baseline_fixture() -> dict[str, Any]:
    return {
        "characters": {"lin": "archive apprentice", "ning": "city guard", "bo": "smuggler"},
        "relationships": {"lin:ning": "uneasy allies", "lin:bo": "debtor"},
        "world_rules": {"salt_lamp": "reveals forged memories", "north_gate": "opens only at midnight"},
        "timeline": ["night-1", "night-2", "night-3"],
        "foreshadows": {"broken_lamp": "planted", "bo_debt": "progressing"},
        "chapters": [
            {"id": "c1", "state": "Lin finds the broken lamp", "relationship": "lin:ning", "text": "Lin hides the broken salt lamp.", "mode": "investigation", "payoff": "", "voice": "Lin: clipped observations", "emotion": "uneasy", "style": "A"},
            {"id": "c2", "state": "Ning blocks the north gate", "relationship": "lin:ning", "text": "Ning blocks the north gate and demands the lamp.", "mode": "confrontation", "payoff": "broken_lamp", "voice": "Ning: direct orders", "emotion": "tense", "style": "A"},
            {"id": "c3", "state": "Bo reveals the debt", "relationship": "lin:bo", "text": "Bo names the debt before midnight.", "mode": "reveal", "payoff": "bo_debt", "voice": "Bo: joking evasions", "emotion": "urgent", "style": "B"},
        ],
    }


def broken_fixture() -> dict[str, Any]:
    fixture = deepcopy(baseline_fixture())
    fixture["chapters"][1]["state"] = ""
    fixture["chapters"][1]["relationship"] = "lin:bo"
    fixture["chapters"][1]["text"] = fixture["chapters"][0]["text"]
    fixture["chapters"][1]["mode"] = "investigation"
    fixture["chapters"][1]["payoff"] = ""
    fixture["chapters"][2]["voice"] = fixture["chapters"][0]["voice"]
    fixture["chapters"][2]["style"] = "A"
    return fixture


def measure_fixture(fixture: dict[str, Any]) -> dict[str, float]:
    chapters = fixture.get("chapters") or []
    total = len(chapters)
    repeats = sum(a.get("text") == b.get("text") for a, b in zip(chapters, chapters[1:]))
    modes = [c.get("mode") for c in chapters]
    return {
        "chapter_state_coverage": round(sum(bool(c.get("state")) for c in chapters) / total, 4) if total else 0.0,
        "repetition_rate": round(repeats / max(total - 1, 1), 4) if total > 1 else 0.0,
        "structural_repeat_rate": round(sum(a == b for a, b in zip(modes, modes[1:])) / max(total - 1, 1), 4) if total > 1 else 0.0,
        "style_contract_coverage": round(sum(bool(c.get("style")) for c in chapters) / total, 4) if total else 0.0,
        "character_voice_separation": round(len(set(c.get("voice", "") for c in chapters)) / total, 4) if total else 0.0,
        "payoff_rate": round(sum(bool(c.get("payoff")) for c in chapters) / max(len(fixture.get("foreshadows") or {}), 1), 4),
    }


def compare_baseline(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(baseline) | set(current))
    return {key: {"baseline": baseline.get(key, 0), "current": current.get(key, 0), "delta": round(float(current.get(key, 0)) - float(baseline.get(key, 0)), 4)} for key in keys}


def detect_regressions(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    base_chapters = {c["id"]: c for c in baseline["chapters"]}
    for current in candidate["chapters"]:
        original = base_chapters[current["id"]]
        if original.get("state") and not current.get("state"):
            issues.append(f"missing_state:{current['id']}")
        if original.get("relationship") != current.get("relationship"):
            issues.append(f"relationship_conflict:{current['id']}")
        if original.get("payoff") and not current.get("payoff"):
            issues.append(f"missing_payoff:{current['id']}")
        if original.get("voice") != current.get("voice"):
            issues.append(f"voice_changed:{current['id']}")
        if original.get("style") != current.get("style"):
            issues.append(f"style_changed:{current['id']}")
    for previous, current in zip(candidate["chapters"], candidate["chapters"][1:]):
        if previous.get("text") == current.get("text"):
            issues.append(f"repeated_chapter_text:{current['id']}")
        if previous.get("mode") == current.get("mode"):
            issues.append(f"repeated_structure:{current['id']}")
    return issues
