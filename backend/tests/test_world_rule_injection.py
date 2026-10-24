"""世界规则是否真的进入正文生成链路（2026-10-22）。

结论先说：**此前只“部分”进入**。
- 三处注入各有一套静默截断：compact 取前 12 条、续写提示取前 12 条、项目上下文取前 8 条；
- 事实核查 prompt 要求“查世界观规则矛盾”，却**从未把规则交给核查员** → 该类检查失效；
- 系统提示里没有“规则是硬约束”的表述，模型容易把它当背景资料。

本文件锁住修复后的行为：全量注入（按字数预算）、超预算显式告知、核查可见、硬约束声明。
"""
from __future__ import annotations

from app.models.schemas import ContinueWritingRequest, WorldRuleContext
from app.services import ai_service, fact_check_service, scene_guidance_service
from app.services.novel_generation_service import _compact_world_rules, _with_world_rules
from app.utils.world_rules import select_world_rules, world_rule_lines


def _rules(count: int, desc_repeat: int = 1) -> list[dict]:
    return [
        {
            "category": "力量体系",
            "name": f"规则{index:02d}",
            "description": ("第%d条世界规则描述。" % index) * desc_repeat,
        }
        for index in range(1, count + 1)
    ]


def test_select_world_rules_accepts_dict_and_pydantic_and_keeps_order():
    """请求体里的规则是 pydantic 对象，项目里存的是 dict —— 两种都必须认。"""
    as_dicts = [{"name": "甲", "description": "描述甲"}, {"name": "乙", "description": "描述乙"}]
    as_models = [WorldRuleContext(**item) for item in as_dicts]

    kept_dicts, omitted_dicts = select_world_rules(as_dicts)
    kept_models, omitted_models = select_world_rules(as_models)

    assert [rule["name"] for rule in kept_dicts] == ["甲", "乙"]
    assert [rule["name"] for rule in kept_models] == ["甲", "乙"]
    assert omitted_dicts == omitted_models == 0


def test_select_world_rules_reports_omitted_instead_of_dropping_silently():
    kept, omitted = select_world_rules(_rules(30, desc_repeat=40), max_chars=400, max_count=30)

    assert 0 < len(kept) < 30
    assert omitted == 30 - len(kept)


def test_world_rule_lines_add_explicit_note_when_over_budget():
    lines = world_rule_lines(_rules(30, desc_repeat=40), max_chars=400, max_count=30)

    assert lines[-1].startswith("- 另有")
    assert "不得自行新增或改写任何世界规则" in lines[-1]


def test_continue_prompt_injects_every_rule_not_just_twelve():
    """旧实现硬取前 12 条：第 13 条起永远进不了 prompt。"""
    req = ContinueWritingRequest(
        projectId="p-rules",
        chapterId="c1",
        cursorPosition=0,
        contextBefore="前文",
        direction="推进冲突",
        wordCount=2000,
        worldRules=[WorldRuleContext(**rule) for rule in _rules(15)],
    )
    req._style_contract = {}  # 跳过 DB 取风格契约

    prompt = ai_service._build_continue_prompt(req)
    injected = [line for line in prompt.splitlines() if line.startswith("- 规则")]

    assert len(injected) == 15
    assert "- 规则15" in prompt


def test_scene_guidance_injects_rules_as_hard_constraint():
    guidance = scene_guidance_service.build_scene_guidance(
        characters=[], world_rules=_rules(15), chapter_contract=None, story_state=None
    )

    assert "世界规则（硬约束，违反即为设定矛盾）" in guidance
    assert len([line for line in guidance.splitlines() if line.startswith("- 规则")]) == 15


def test_compact_world_rules_no_longer_truncates():
    assert len(_compact_world_rules(_rules(15))) == 15


def test_fact_check_context_carries_world_rules():
    """核查 prompt 第 5 类要查“设定矛盾”，必须真的把规则喂进去。"""
    context = fact_check_service.build_fact_check_context(_with_world_rules({"recent_chapter_summaries": ["上一章摘要"]}, _rules(5)))

    assert "【世界规则（硬约束）】" in context
    assert len([line for line in context.splitlines() if line.startswith("- 规则")]) == 5
    assert "上一章摘要" in context  # 原有材料不受影响


def test_fact_check_context_unchanged_without_world_rules():
    context = fact_check_service.build_fact_check_context({"recent_chapter_summaries": ["上一章摘要"]})

    assert "世界规则" not in context


def test_continue_system_prompt_declares_rules_as_hard_constraint():
    prompt = ai_service.CONTINUE_SYSTEM_PROMPT

    assert "硬约束" in prompt
    assert "改情节，不改规则" in prompt
