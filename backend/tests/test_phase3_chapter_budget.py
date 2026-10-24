"""Phase 3: hard budget gates are deterministic and never invoke a provider late."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.chapter_budget_service import ChapterGenerationBudget
from app.services import fact_check_service
from app.services import hook_service, rewrite_service
from app.services import review_service


def test_budget_consumes_before_the_call_and_returns_auditable_allowance():
    budget = ChapterGenerationBudget(max_qa_calls=1, max_rewrite_calls=0)
    allowance = budget.allow_llm_call("fact_check", 800)
    assert allowance and allowance.before == 0 and allowance.after == 1
    assert budget.allow_llm_call("fact_escalation", 800) is None


@pytest.mark.asyncio
async def test_fact_check_does_not_call_provider_after_budget_is_exhausted():
    gate = lambda _operation, _tokens: False
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock()) as chat:
        result = await fact_check_service.check_chapter("p", "c", "正文", allow_llm_call=gate)
    assert result["budget_exhausted"] is True
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_fact_check_loop_stops_normally_when_shared_budget_is_exhausted():
    budget = ChapterGenerationBudget(max_qa_calls=1, max_rewrite_calls=0)
    gate = lambda operation, tokens: budget.allow_llm_call(operation, tokens) is not None
    failing = '{"result":"FAIL","issues":[{"category":"设定","detail":"冲突","fix":"修复"}]}'
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(return_value=failing)) as chat:
        result = await fact_check_service.run_fact_check_loop("p", "c", "正文", max_retries=1, allow_llm_call=gate)
    assert result["status"] == "budget_exhausted"
    assert budget.qa_calls == 1
    assert chat.await_count == 1


@pytest.mark.asyncio
async def test_qa_zero_never_reaches_fact_or_hook_provider():
    budget = ChapterGenerationBudget(max_qa_calls=0)
    gate = lambda operation, tokens: budget.allow_llm_call(operation, tokens) is not None
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock()) as fact_chat, patch.object(hook_service.llm, "chat", new=AsyncMock()) as hook_chat:
        fact = await fact_check_service.check_chapter("p", "c", "正文", allow_llm_call=gate)
        hook = await hook_service.check_hook("正文", gate)
    assert fact["budget_exhausted"] and hook["budget_exhausted"]
    fact_chat.assert_not_awaited()
    hook_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_with_qa_one_cannot_make_a_second_provider_call():
    budget = ChapterGenerationBudget(max_qa_calls=1)
    gate = lambda operation, tokens: budget.allow_llm_call(operation, tokens) is not None
    low_confidence = '{"result":"PASS", "confidence": 0.1, "issues": []}'
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(return_value=low_confidence)) as chat:
        result = await fact_check_service.check_chapter("p", "c", "正文", allow_llm_call=gate)
    assert result["budget_exhausted"] is True
    assert chat.await_count == 1


@pytest.mark.asyncio
async def test_rewrite_budget_zero_blocks_direct_style_provider_call():
    budget = ChapterGenerationBudget(max_rewrite_calls=0)
    gate = lambda operation, tokens: budget.allow_llm_call(operation, tokens) is not None
    with patch.object(rewrite_service.llm, "chat", new=AsyncMock()) as chat:
        result = await rewrite_service._rewrite_section_llm("x" * 800, rewrite_service.REWRITE_TYPE_DIALOGUE, gate)
    assert result == "x" * 800
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_contract_review_with_qa_zero_never_reaches_provider():
    budget = ChapterGenerationBudget(max_qa_calls=0)
    gate = lambda operation, tokens: budget.allow_llm_call(operation, tokens) is not None
    with patch.object(review_service.llm, "chat", new=AsyncMock()) as chat:
        result = await review_service.review_logic("正文", "c", allow_llm_call=gate)
    assert result["budget_exhausted"] is True
    chat.assert_not_awaited()


def test_generation_and_retry_limits_reject_the_next_attempt():
    budget = ChapterGenerationBudget(max_generation_calls=2, max_retry_calls=1)
    assert budget.allow_llm_call("generation") is not None
    assert budget.allow_llm_call("generation") is not None
    assert budget.allow_llm_call("generation") is None
    assert budget.generation_calls == 2
    assert budget.allow_llm_call("retry") is not None
    assert budget.allow_llm_call("retry") is None
