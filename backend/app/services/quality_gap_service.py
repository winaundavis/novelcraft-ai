"""Zero-API Phase 8 fixture for comparing scene-writing prompt strategies."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


FIXTURE = Path(__file__).resolve().parents[2] / "experiment_fixtures" / "phase8" / "quality_scene.json"
_EXPOSITION_MARKERS = ("解释", "因为", "原因", "意味着", "叙述者")


def _scene() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _baseline_prompt(scene: dict[str, Any]) -> str:
    return "\n".join(
        [
            "只写中文小说正文，不要解释创作过程。",
            f"故事：{scene['story']}",
            f"世界规则：{scene['world_rule']}",
            f"本章目标：{scene['scene_goal']}",
            f"本章障碍：{scene['external_obstacle']}",
        ]
    )


def _quality_layer(scene: dict[str, Any]) -> str:
    fields = ("scene_goal", "physical_action", "sensory_anchor", "character_intention", "external_obstacle", "emotional_shift", "subtext", "scene_exit")
    labels = {
        "scene_goal": "场景目标", "physical_action": "物理动作", "sensory_anchor": "感官锚点",
        "character_intention": "人物意图", "external_obstacle": "外部障碍", "emotional_shift": "情绪变化",
        "subtext": "对话潜台词", "scene_exit": "场景出口",
    }
    details = [f"{labels[key]}：{scene[key]}" for key in fields]
    return "\n".join(
        [
            "【场景具身合同】",
            *details,
            "优先通过动作、环境、感官与选择表现情绪、关系、冲突和紧张程度。",
            "只解释不说会造成逻辑断裂的规则或背景；不要连续用因果说明替代场景体验。",
            "关键冲突对话允许保留未说出口的真实目的，不要把潜台词逐句写明。",
            f"【章末合同】至少以以下一种可感知变化结束：{'、'.join(scene['ending_contract'])}。不要求 cliffhanger。",
        ]
    )


def prepare_quality_gap_fixture() -> dict[str, Any]:
    """Prepare two fixed prompt variants without an API call or production mutation."""
    scene = _scene()
    baseline = _baseline_prompt(scene)
    candidate = baseline + "\n\n" + _quality_layer(scene)
    return {
        "real_llm_calls": 0,
        "call_plan": {"primary_calls": 2, "retries": 0, "rewrites": 0, "qa": 0},
        "requests": {
            "baseline": {"scene": scene, "quality_layer": False, "user_prompt": baseline},
            "candidate": {"scene": scene, "quality_layer": True, "user_prompt": candidate},
        },
        "human_questions": ["哪个更生动？", "哪个更有画面？", "哪个更沉浸？", "哪个更自然？", "哪个更想继续读？"],
    }


def evaluate_quality_signals(text: str, ending_contract: list[str]) -> dict[str, Any]:
    """Return deterministic warnings only; no literary-quality score is inferred."""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", str(text or "")) if item.strip()]
    run = 0
    longest = 0
    for paragraph in paragraphs:
        if any(marker in paragraph for marker in _EXPOSITION_MARKERS):
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    evidence = [
        kind for kind in ending_contract or []
        if f"章末证据：{kind}" in str(text or "")
    ]
    return {
        "exposition_risk": longest >= 3,
        "exposition_paragraph_run": longest,
        "ending_contract_evidence": evidence,
        "is_literary_judgment": False,
    }
