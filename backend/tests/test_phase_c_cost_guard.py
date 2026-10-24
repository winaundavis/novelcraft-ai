"""Phase C: per-chapter generation cost and expansion guard (zero API calls)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models.schemas import ContinueWritingRequest
from app.services import ai_service, novel_generation_service as ngs


def test_chapter_budget_tracks_each_budget_dimension():
    budget = ngs.ChapterGenerationBudget()

    assert budget.consume("generation") is True
    assert budget.generation_calls == 1
    assert budget.consume("retry") is True
    assert budget.retry_calls == 1
    assert budget.consume("qa") is True
    assert budget.qa_calls == 1
    assert budget.consume("rewrite") is True
    assert budget.rewrite_calls == 1


def test_chapter_budget_keeps_word_ranges():
    budget = ngs.ChapterGenerationBudget(
        target_words=2000, acceptable_words=1800, minimum_words=1500
    )
    assert budget.to_dict()["target_words"] == 2000
    assert budget.to_dict()["acceptable_words"] == 1800
    assert budget.to_dict()["minimum_words"] == 1500


def test_chapter_budget_rejects_the_next_call_and_is_auditable():
    budget = ngs.ChapterGenerationBudget(max_generation_calls=1)

    assert budget.consume("generation") is True
    assert budget.consume("generation") is False
    snapshot = budget.to_dict()
    assert snapshot["generation_calls"] == 1
    assert snapshot["max_generation_calls"] == 1


@pytest.mark.parametrize("operation, dimension, maximum", [
    ("generation", "generation", "max_generation_calls"),
    ("retry", "retry", "max_retry_calls"),
    ("fact_check", "qa", "max_qa_calls"),
    ("ai_rewrite", "rewrite", "max_rewrite_calls"),
])
def test_budget_exhaustion_blocks_provider_reservation(operation, dimension, maximum):
    """All provider-backed operations share a deterministic pre-call gate."""
    budget = ngs.ChapterGenerationBudget(**{maximum: 0})
    assert budget.allow_llm_call(operation, estimated_tokens=100) is None
    assert getattr(budget, f"{dimension}_calls") == 0


@pytest.mark.asyncio
async def test_expansion_is_skipped_for_acceptable_shortfall():
    job = ngs.NovelGenerationJob(
        id="job-c", project_id="p", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=2000, total_chapters=1,
    )
    chapter = {"id": "c1", "title": "第一章", "targetWordCount": 2000}
    generated = {"text": "a" * 1850, "source": "llm", "provider": "x", "model": "m"}
    with patch.object(ngs, "_load_existing_chapter_text", return_value=""), \
         patch.object(ngs.chapter_contract_service, "get_or_compile_chapter_contract", return_value={}), \
         patch.object(ngs, "generate_continuation_result", new=AsyncMock(return_value=generated)) as call:
        result = await ngs._generate_chapter_with_expansion(
            job=job, chapter=chapter, chapter_index=0, total_chapters=1,
            premise="p", outline_summary="o", characters=[], world_rules=[],
        )
    assert len(result["content"]) == 1850
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_generation_accepts_output_above_hard_minimum_without_expansion():
    """Removing the single-mode branch would spend an unauthorized second call."""
    job = ngs.NovelGenerationJob(
        id="job-single", project_id="p", mode="single", requested_chapter_id="c1",
        status="running", message="", target_words=2500, total_chapters=1,
    )
    chapter = {"id": "c1", "title": "第一章", "targetWordCount": 2500}
    generated = {
        "text": "甲" * 2146,
        "source": "llm",
        "provider": "arts",
        "model": "deepseek-v4-flash",
    }
    budget = ngs._chapter_budget_for_job(job, 2500)
    with patch.object(ngs, "_load_existing_chapter_text", return_value=""), \
         patch.object(ngs.chapter_contract_service, "get_or_compile_chapter_contract", return_value={}), \
         patch.object(ngs, "generate_continuation_result", new=AsyncMock(return_value=generated)) as call:
        result = await ngs._generate_chapter_with_expansion(
            job=job, chapter=chapter, chapter_index=0, total_chapters=1,
            premise="p", outline_summary="o", characters=[], world_rules=[], budget=budget,
        )

    assert len(result["content"]) == 2146
    assert call.await_count == 1
    assert budget.generation_calls == 1


@pytest.mark.asyncio
async def test_single_generation_below_hard_minimum_stays_uncommitted_without_expansion():
    """A truly short single response must fail without spending a second call."""
    job = ngs.NovelGenerationJob(
        id="job-single-short", project_id="p", mode="single", requested_chapter_id="c1",
        status="running", message="", target_words=2500, total_chapters=1,
    )
    chapter = {"id": "c1", "title": "第一章", "targetWordCount": 2500}
    generated = {
        "text": "甲" * 1800,
        "source": "llm",
        "provider": "arts",
        "model": "deepseek-v4-flash",
    }
    budget = ngs._chapter_budget_for_job(job, 2500)
    with patch.object(ngs, "_load_existing_chapter_text", return_value=""), \
         patch.object(ngs.chapter_contract_service, "get_or_compile_chapter_contract", return_value={}), \
         patch.object(ngs, "generate_continuation_result", new=AsyncMock(return_value=generated)) as call:
        with pytest.raises(ngs.GenerationIncompleteError, match="1800/1875"):
            await ngs._generate_chapter_with_expansion(
                job=job, chapter=chapter, chapter_index=0, total_chapters=1,
                premise="p", outline_summary="o", characters=[], world_rules=[], budget=budget,
            )

    assert call.await_count == 1
    assert budget.generation_calls == 1
    assert job.preview_content == "甲" * 1800


@pytest.mark.asyncio
async def test_expansion_carries_contract_and_rejects_high_repeat():
    job = ngs.NovelGenerationJob(
        id="job-c", project_id="p", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=2000, total_chapters=1,
    )
    chapter = {"id": "c1", "title": "第一章", "targetWordCount": 2000}
    contract = {"goal": "推进冲突", "chapter_mode": "Conflict", "must_payoff": ["线索"]}
    initial = {"text": "a" * 1000, "source": "llm", "provider": "x", "model": "m"}
    repeated = "a" * 60
    with patch.object(ngs, "_load_existing_chapter_text", return_value=""), \
         patch.object(ngs.chapter_contract_service, "get_or_compile_chapter_contract", return_value=contract), \
         patch.object(ngs, "generate_continuation_result", new=AsyncMock(side_effect=[initial, {"text": repeated, "source": "llm"}])) as call, \
         patch.object(ngs.review_service, "_cross_chapter_ngram_repeat", return_value=[{"phrase": repeated, "length": 60}]), \
         patch.object(ngs, "_save_job"):
        with pytest.raises(ngs.ChapterBudgetExceeded, match="repeated"):
            await ngs._generate_chapter_with_expansion(
                job=job, chapter=chapter, chapter_index=0, total_chapters=1,
                premise="p", outline_summary="o", characters=[], world_rules=[],
            )
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_formal_generation_and_expansion_keep_hard_truth_above_soft_craft():
    job = ngs.NovelGenerationJob(
        id="job-guidance", project_id="p", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=2000, total_chapters=1,
    )
    chapter = {"id": "c1", "title": "第一章", "targetWordCount": 2000}
    contract = {"goal": "交付盐灯", "conflict": "封桥", "state_change": "梅苏知道代价"}
    character = {"name": "林乔", "gender": "female", "basicInfo": {"identity": "信使"}}
    world_rule = {"name": "盐灯", "description": "打开会夺走无关者的记忆"}
    generated = [
        {"text": "甲" * 1000, "source": "llm", "provider": "x", "model": "m"},
        {"text": "乙" * 1000, "source": "llm", "provider": "x", "model": "m"},
    ]
    with patch.object(ngs, "_load_existing_chapter_text", return_value=""), \
         patch.object(ngs.chapter_contract_service, "get_or_compile_chapter_contract", return_value=contract), \
         patch.object(ngs.story_state_service, "get_story_state", return_value={"relationship_changes": ["林乔信任梅苏"]}), \
         patch.object(ngs, "generate_continuation_result", new=AsyncMock(side_effect=generated)) as call:
        await ngs._generate_chapter_with_expansion(
            job=job, chapter=chapter, chapter_index=0, total_chapters=1,
            premise="p", outline_summary="o", characters=[character], world_rules=[world_rule],
        )

    assert call.await_count == 2
    for request in (call.await_args_list[0].args[0], call.await_args_list[1].args[0]):
        assert "【Hard Story Truth】" in request.direction
        assert "林乔：身份=信使；性别=female" in request.direction
        assert "盐灯：打开会夺走无关者的记忆" in request.direction
        assert "【Scene Plan / Soft Craft Guidance】" in request.direction


@pytest.mark.asyncio
async def test_formal_generation_request_does_not_receive_future_arc_plot():
    job = ngs.NovelGenerationJob(
        id="job-arc-scope", project_id="p", mode="single", requested_chapter_id="c1",
        status="running", message="", target_words=800, total_chapters=1,
    )
    chapter = {
        "id": "c1", "title": "第一章", "targetWordCount": 800,
        "summary": "旧摘要错误包含第二弧血雨和黑塔。",
    }
    contract = {
        "goal": "只写白菜枕头的古怪日常",
        "conflict": "梧渡把异常视为平常",
        "state_change": "梧渡仍未察觉异常",
        "must_avoid": ["不得提前进入后续剧情弧"],
        "arc_context": {
            "outline_scope": {
                "volume_title": "第一卷",
                "arc_title": "白菜枕头",
                "arc_summary": "梧渡只经历白菜枕头的古怪日常，并未察觉异常。",
                "has_later_arcs": True,
            }
        },
    }
    generated = {"text": "甲" * 800, "source": "llm", "provider": "x", "model": "m"}
    with patch.object(ngs, "_load_existing_chapter_text", return_value=""), \
         patch.object(ngs.chapter_contract_service, "get_or_compile_chapter_contract", return_value=contract), \
         patch.object(ngs.story_state_service, "get_story_state", return_value={}), \
         patch.object(ngs, "_minimum_acceptable_words", return_value=1), \
         patch.object(ngs, "generate_continuation_result", new=AsyncMock(return_value=generated)) as call:
        await ngs._generate_chapter_with_expansion(
            job=job,
            chapter=chapter,
            chapter_index=0,
            total_chapters=1,
            premise="梧渡生活在认知异常的小城",
            outline_summary="第二弧发生血雨，随后发现黑塔，最终世界崩坏。",
            characters=[{"name": "梧渡", "role": "protagonist", "arc": "血雨之后觉醒并进入黑塔"}],
            world_rules=[],
        )

    request = call.await_args.args[0]
    combined = "\n".join((
        request.contextBefore,
        request.direction,
        request.outlineSummary or "",
        request.chapterSummary or "",
        ai_service._build_continue_prompt(request),
    ))
    assert "白菜枕头" in combined
    assert "血雨" not in combined
    assert "黑塔" not in combined
    assert "世界崩坏" not in combined


def test_expansion_direction_contains_rendered_contract():
    direction = ngs._build_expansion_direction(
        chapter={"title": "第一章", "summary": "推进"},
        actual_words=1000, target_words=2000, expansion_pass=1,
        chapter_contract={"goal": "推进冲突", "chapter_mode": "Conflict"},
    )
    assert "推进冲突" in direction
