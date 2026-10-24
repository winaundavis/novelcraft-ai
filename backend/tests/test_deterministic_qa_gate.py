from unittest.mock import AsyncMock, patch

import pytest

from app.services import deterministic_qa_service as qa
from app.services import novel_generation_service as ngs
from app.services import review_service


def _contract(**overrides):
    value = {"goal": "推进调查", "conflict": "守卫阻拦", "state_change": "获得钥匙", "must_payoff": ["旧信"], "ending_hook": "门后有人", "chapter_mode": "Investigation", "version": 2}
    value.update(overrides)
    return value


def test_layer1_blocks_empty_missing_contract_and_order_gap():
    result = qa.evaluate_chapter(content="", chapter={"id": "c3", "title": "第三章", "order": 3}, contract=None, previous_chapters=[{"id": "c1", "title": "第一章", "order": 1}])
    codes = {item["code"] for item in result["blocking"]}
    assert {"empty_chapter", "missing_contract", "chapter_order_gap"} <= codes
    assert result["decision"] == "pause"


def test_explicit_range_start_can_allow_prior_order_gap():
    result = qa.evaluate_chapter(
        content="沈砚走进禁地，石门在身后闭合。" * 30,
        chapter={"id": "c3", "title": "第三章", "order": 3},
        contract=_contract(must_payoff=[]),
        previous_chapters=[{"id": "c1", "title": "第一章", "order": 1}],
        allow_order_gap=True,
    )
    assert "chapter_order_gap" not in {item["code"] for item in result["blocking"]}


def test_layer2_flags_possible_repetition_without_claiming_literary_failure():
    text = ("他推门而入，看见桌上的旧信。" * 30) + "\n\n" + ("他沉默。" * 20)
    result = qa.evaluate_chapter(content=text, chapter={"id": "c2", "title": "旧信", "order": 2}, contract=_contract(), previous_chapters=[{"id": "c1", "title": "前章", "order": 1, "content": text}])
    assert result["layer2"]["status"] == "possible_issue"
    assert result["layer2"]["is_literary_judgment"] is False
    assert any(item["code"] in {"repeated_text", "recent_structure_similarity", "sentence_repetition"} for item in result["warnings"])


def test_risk_budget_is_zero_one_or_two():
    low = qa.evaluate_chapter(content="动作推进。" * 700, chapter={"id": "c1", "title": "过渡", "order": 1}, contract=_contract(chapter_mode="Transition", must_payoff=[]), previous_chapters=[])
    medium = qa.evaluate_chapter(content="调查推进。" * 700, chapter={"id": "c2", "title": "调查", "order": 2}, contract=_contract(), previous_chapters=[])
    high = qa.evaluate_chapter(content="大战后主角死亡，世界规则揭晓。" * 700, chapter={"id": "c3", "title": "终战", "order": 3}, contract=_contract(chapter_mode="Climax", key_events=["主角死亡", "世界真相揭示"]), previous_chapters=[])
    assert low["risk"]["llm_qa_budget"] == 0
    assert medium["risk"]["llm_qa_budget"] == 1
    assert high["risk"]["llm_qa_budget"] == 2


def test_required_payoff_and_closed_foreshadow_are_checked_deterministically():
    result = qa.evaluate_chapter(content="本章只写了赶路。" * 300, chapter={"id": "c2", "title": "赶路", "order": 2}, contract=_contract(), previous_chapters=[], foreshadows=[{"name": "旧信", "status": "resolved"}], active_foreshadow_names=["旧信"])
    codes = {item["code"] for item in result["warnings"]}
    assert "payoff_evidence_missing" in codes
    assert "resolved_foreshadow_active" in codes


def test_layer1_blocks_structured_fact_timeline_name_and_state_conflicts():
    text = ("林川在景历十三年抵达北城，同行者林州仍然活着。" * 20)
    result = qa.evaluate_chapter(
        content=text,
        chapter={"id": "c2", "title": "北城", "order": 2},
        contract=_contract(characters=["林川"], must_payoff=[]),
        previous_chapters=[{"id": "c1", "title": "出发", "order": 1}],
        character_name_rules=[{"canonical": "林川", "invalid_variants": ["林州"]}],
        known_fact_rules=[{"fact": "林川已经失去右眼", "contradictions": ["双眼完好"]}],
        timeline_rules=[{"event": "当前纪年为景历十二年", "forbidden_terms": ["景历十三年"]}],
        expected_state_version=4,
        actual_state_version=3,
    )
    codes = {item["code"] for item in result["blocking"]}
    assert {"character_name_inconsistency", "timeline_conflict", "state_version_gap"} <= codes
    assert "known_fact_conflict" not in codes
    assert result["decision"] == "pause"


def test_layer1_blocks_known_fact_conflict_only_when_explicit_rule_matches():
    result = qa.evaluate_chapter(
        content=("林川醒来后确认自己双眼完好。" * 30),
        chapter={"id": "c1", "title": "苏醒", "order": 1},
        contract=_contract(characters=["林川"], must_payoff=[]),
        previous_chapters=[],
        known_fact_rules=[{"fact": "林川已经失去右眼", "contradictions": ["双眼完好"]}],
    )
    assert "known_fact_conflict" in {item["code"] for item in result["blocking"]}


def test_layer2_flags_mode_frequency_dialogue_and_character_evidence_as_possible_only():
    text = ("钟声再次响起，长廊尽头的钟声催促众人前行。" * 35)
    previous = [
        {"id": f"c{i}", "title": f"前章{i}", "order": i, "chapter_mode": "Transition", "content": "不同内容。" * 60}
        for i in range(1, 4)
    ]
    result = qa.evaluate_chapter(
        content=text,
        chapter={"id": "c4", "title": "钟声", "order": 4},
        contract=_contract(chapter_mode="Transition", characters=["林川"], must_payoff=[]),
        previous_chapters=previous,
    )
    codes = {item["code"] for item in result["warnings"]}
    assert {
        "chapter_mode_repetition",
        "dialogue_ratio_anomaly",
        "high_frequency_term",
        "character_evidence_missing",
    } <= codes
    assert result["layer2"] == {"status": "possible_issue", "is_literary_judgment": False}
    assert result["decision"] == "continue"


def _job() -> ngs.NovelGenerationJob:
    return ngs.NovelGenerationJob(
        id="qa-job",
        project_id="qa-project",
        mode="all",
        requested_chapter_id=None,
        status="running",
        message="",
        target_words=0,
        total_chapters=1,
    )


async def _run_gated(contract, content="林川沿着石阶前行。" * 40):
    return await ngs._run_self_check_loop(
        job=_job(),
        chapter={"id": "c1", "title": "第一章", "order": 1},
        result={"content": content, "chapter_contract": contract},
        memory_context=None,
        previous_id=None,
        next_id="c2",
        hook_instruction="",
    )


@pytest.mark.asyncio
async def test_low_risk_contract_uses_zero_llm_reviews():
    with (
        patch.object(ngs.chapters_dao, "list_chapters_global_order", return_value=[]),
        patch.object(ngs.foreshadows_dao, "list_foreshadows", return_value=[]),
        patch.object(review_service, "review_logic", new=AsyncMock()) as logic,
        patch.object(review_service, "review_language", new=AsyncMock()) as language,
    ):
        result = await _run_gated(_contract(chapter_mode="Transition", must_payoff=[]))
    assert result["llm_qa_calls"] == 0
    logic.assert_not_awaited()
    language.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "expected"), [("Conflict", 1), ("Climax", 2)])
async def test_medium_and_high_risk_enforce_review_budget(mode, expected):
    with (
        patch.object(ngs.chapters_dao, "list_chapters_global_order", return_value=[]),
        patch.object(ngs.foreshadows_dao, "list_foreshadows", return_value=[]),
        patch.object(review_service, "review_logic", new=AsyncMock(return_value={"issues": []})) as logic,
        patch.object(review_service, "review_language", new=AsyncMock(return_value={"issues": []})) as language,
    ):
        result = await _run_gated(_contract(chapter_mode=mode, must_payoff=[]))
    assert result["llm_qa_calls"] == expected
    assert logic.await_count == 1
    assert language.await_count == expected - 1


@pytest.mark.asyncio
async def test_blocking_gate_prevents_llm_review():
    with (
        patch.object(ngs.chapters_dao, "list_chapters_global_order", return_value=[]),
        patch.object(ngs.foreshadows_dao, "list_foreshadows", return_value=[]),
        patch.object(review_service, "review_logic", new=AsyncMock()) as logic,
    ):
        with pytest.raises(RuntimeError, match="确定性 QA Gate"):
            await _run_gated(_contract(chapter_mode="Climax"), content="太短")
    logic.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_contract_story_state_version_prevents_llm_review():
    contract = _contract(chapter_mode="Climax", story_state_version=2)
    with (
        patch.object(ngs.chapters_dao, "list_chapters_global_order", return_value=[]),
        patch.object(ngs.foreshadows_dao, "list_foreshadows", return_value=[]),
        patch.object(ngs.story_state_service, "get_story_state", return_value={"version": 3}),
        patch.object(review_service, "review_logic", new=AsyncMock()) as logic,
    ):
        with pytest.raises(RuntimeError, match="Story State 版本不连续"):
            await _run_gated(contract)
    logic.assert_not_awaited()
