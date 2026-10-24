"""Deterministic character voice profiles; no model call is required to seed them."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.dao import character_voice as voice_dao

VOICE_RENDER_BUDGET = 1600


def _character_id(character: dict[str, Any]) -> str:
    return str(character.get("id") or character.get("name") or "").strip()


def _seed(character: dict[str, Any]) -> dict[str, Any]:
    speech = str(character.get("speechStyle") or character.get("speech_style") or "").strip()
    desire = str(character.get("deepDesire") or character.get("surfaceMotivation") or "").strip()
    habits = str(character.get("habits") or "").strip()
    catchphrase = str(character.get("catchphrase") or "").strip()
    return {
        "worldview": desire or str(character.get("role") or "从自身立场判断").strip(),
        "speech_length": "短句" if "短" in speech else "长句" if "长" in speech else "适中",
        "sentence_pattern": speech or "根据压力改变句式，避免与其他角色同声线",
        "favorite_expressions": [catchphrase] if catchphrase else [],
        "avoided_expressions": [],
        "emotional_expression": "通过行动和反应表现情绪",
        "information_strategy": "先隐藏动机，再用行动透露信息",
        "behavior_signature": habits or "用可观察动作表现状态",
    }


def get_or_seed_profiles(project: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        existing = voice_dao.list_profiles(project["id"])
        known = {item["character_id"] for item in existing}
        for character in project.get("characters") or []:
            character_id = _character_id(character)
            if character_id and character_id not in known:
                seeded = _seed(character)
                created = voice_dao.create_profile(project["id"], character_id, str(character.get("name") or character_id), seeded)
                existing.append(created if isinstance(created, dict) else {"project_id": project["id"], "character_id": character_id, "name": str(character.get("name") or character_id), **seeded})
        return existing
    except sqlite3.OperationalError:
        return []


def render_voice_profiles(project: dict[str, Any], character_names: list[str] | None = None, *, include_plot: bool = True) -> str:
    profiles = get_or_seed_profiles(project)
    wanted = set(character_names or [])
    if wanted:
        profiles = [item for item in profiles if item.get("name") in wanted or item.get("character_id") in wanted]
    lines = ["【人物声线】"]
    for item in profiles[:6]:
        plot = f"世界观={item.get('worldview') or ''}；" if include_plot else ""
        strategy = f"信息策略={item.get('information_strategy') or ''}；" if include_plot else ""
        lines.append(
            f"- {item.get('name') or item.get('character_id')}: {plot}"
            f"句式={item.get('sentence_pattern') or ''}；情绪={item.get('emotional_expression') or ''}；"
            f"{strategy}行为签名={item.get('behavior_signature') or ''}"
        )
    return "\n".join(lines)[:VOICE_RENDER_BUDGET] if len(lines) > 1 else ""
