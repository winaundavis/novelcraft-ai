"""Pure Phase 8-B A/B/C quality-fixture prompt assembly."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


FIXTURE = Path(__file__).resolve().parents[2] / "experiment_fixtures" / "phase8b" / "quality_scene.json"


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _hard_quality_layer(scene: dict[str, Any]) -> str:
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


def _canonical_fact_guard(facts: dict[str, Any]) -> str:
    character_lines = [f"{row['display_name']}：{row['gender']}" for row in facts["characters"]]
    return "\n".join(
        [
            "【Canonical Facts】",
            "角色：",
            *character_lines,
            "世界：",
            *facts["world_rules"],
            f"当前目标：{facts['scene_goal']}",
            f"当前障碍：{facts['external_obstacle']}",
            "质量层不得修改以上事实。",
        ]
    )


def _soft_quality_layer(scene: dict[str, Any]) -> str:
    return "\n".join(
        [
            "【场景合同】",
            "【Hard】",
            f"场景目标：{scene['scene_goal']}",
            "角色身份、世界规则与外部障碍以 Canonical Facts 为准。",
            "【Soft】",
            f"感官锚点：{scene['sensory_anchor']}",
            f"物理动作：{scene['physical_action']}",
            f"情绪变化：{scene['emotional_shift']}",
            f"对话潜台词：{scene['subtext']}",
            "优先考虑使用动作、环境、感官与人物选择增强体验；在不破坏既有事实、人物一致性和自然叙事的前提下使用。",
            "Soft 不得覆盖 Hard。",
            "不要新增当前上下文未提供、且会改变世界规则、人物背景或剧情事实的重要设定。",
            "【Exit】",
            f"结尾方向：{scene['scene_exit']}；可感知变化可为：{'、'.join(scene['ending_contract'])}。不要求 cliffhanger。",
        ]
    )


def _request(scene: dict[str, Any], facts: dict[str, Any], variant: str, prompt: str, quality_layer: str) -> dict[str, Any]:
    return {
        "variant": variant,
        "scene": scene,
        "canonical_facts": facts,
        "scene_hash": _hash(scene),
        "fact_hash": _hash(facts),
        "quality_layer": quality_layer,
        "user_prompt": prompt,
    }


def prepare_phase8b_fixture() -> dict[str, Any]:
    """Build fixed A/B/C requests without database mutation or API access."""
    scene = _scene()
    facts = scene["canonical_facts"]
    baseline = _baseline_prompt(scene)
    return {
        "real_llm_calls": 0,
        "call_plan": {"primary_calls": 3, "retries": 0, "rewrites": 0, "qa": 0},
        "requests": {
            "A": _request(scene, facts, "A", baseline, "none"),
            "B": _request(scene, facts, "B", baseline + "\n\n" + _hard_quality_layer(scene), "hard_quality"),
            "C": _request(scene, facts, "C", baseline + "\n\n" + _canonical_fact_guard(facts) + "\n\n" + _soft_quality_layer(scene), "soft_quality_with_fact_guard"),
        },
        "human_questions": ["哪个更生动？", "哪个更有画面？", "哪个更沉浸？", "哪个更自然？", "哪个更想继续读？", "事实错误？", "是否发明额外设定？"],
    }
