from __future__ import annotations

import pytest

from app.models.schemas import ChatRequest, GenerateOutlineRequest


def _request(text: str) -> ChatRequest:
    return ChatRequest(projectId="p1", messages=[{"role": "user", "content": text}])


def _outline_request() -> GenerateOutlineRequest:
    return GenerateOutlineRequest(
        projectId="p1",
        premise="一个人在陌生城市追查一段被隐瞒的往事",
        targetChapters=6,
    )


@pytest.mark.asyncio
async def test_generate_outline_fallback_exposes_safe_reason(monkeypatch):
    from app.services import ai_service

    class FailingProvider:
        provider = "arts"
        model = "deepseek-v4-flash"

        async def chat(self, *_args, **_kwargs):
            raise ai_service.LLMResponseError("upstream payload invalid")

    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: FailingProvider())
    monkeypatch.setattr(ai_service, "clear_llm_call_meta", lambda: None)
    result = await ai_service.generate_outline(_outline_request())

    assert result["mode"] == "fallback"
    assert result["is_local_suggestion"] is True
    assert result["error_code"] == "response_invalid"
    assert result["trace_id"]


@pytest.mark.asyncio
async def test_generate_outline_success_is_marked_as_model_result(monkeypatch):
    from app.services import ai_service

    class WorkingProvider:
        provider = "arts"
        model = "deepseek-v4-flash"

        async def chat(self, *_args, **_kwargs):
            return '{"mainSummary":"一段有效的大纲摘要，包含冲突、转折和结果。","volumes":[{"title":"第一卷","summary":"建立冲突并留下悬念。","arcs":[{"title":"起点","summary":"主角发现异常并决定追查。","chapterCount":6}]}]}'

    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: WorkingProvider())
    monkeypatch.setattr(ai_service, "clear_llm_call_meta", lambda: None)
    monkeypatch.setattr(ai_service, "get_llm_call_meta", lambda: {"audit_id": "outline-audit-1"})
    result = await ai_service.generate_outline(_outline_request())

    assert result["mode"] == "llm"
    assert result["is_local_suggestion"] is False
    assert result["error_code"] is None
    assert result["trace_id"] == "outline-audit-1"


@pytest.mark.asyncio
async def test_unconfigured_provider_returns_explicit_local_suggestion_mode(monkeypatch):
    from app.services import ai_service

    class UnconfiguredProvider:
        provider = "arts"
        model = "deepseek-v4-flash"

        async def chat(self, *_args, **_kwargs):
            raise ai_service.LLMConfigurationError("provider missing")

    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: UnconfiguredProvider())
    monkeypatch.setattr(ai_service.project_context_service, "assemble_project_context", lambda **_: {"text": "", "profile": "", "chars": [], "budget": {}, "blocks": []})
    result = await ai_service.chat_reply(_request("\u8bf7\u7ed9\u6211\u4e00\u4e9b\u5927\u7eb2\u5efa\u8bae"))
    assert result["mode"] == "fallback"
    assert result["is_local_suggestion"] is True
    assert result["error_code"] == "provider_not_configured"


def test_classify_chat_intent_detects_outline_edit_requests():
    from app.services.ai_service import classify_chat_intent

    assert classify_chat_intent([{"role": "user", "content": "\u8bf7\u628a\u7b2c\u4e00\u5377\u62c6\u6210\u4e24\u4e2a\u5267\u60c5\u5f27"}], None) == "outline_edit"
    assert classify_chat_intent([{"role": "user", "content": "\u7b2c\u4e00\u5377\u73b0\u5728\u7684\u95ee\u9898\u662f\u4ec0\u4e48\uff1f"}], None) == "consultation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_name", "error_code", "message_fragment", "retryable"),
    [
        ("LLMBudgetExceededError", "budget_exhausted", "额度", False),
        ("LLMTimeoutError", "provider_timeout", "超时", True),
        ("LLMTransportError", "provider_unreachable", "连接", True),
        ("LLMResponseError", "response_invalid", "无效", True),
    ],
)
async def test_chat_reply_reports_the_real_failure_category(
    monkeypatch, exception_name, error_code, message_fragment, retryable
):
    """A budget/transport failure must not be mislabeled as an invalid model response."""
    from app.llm import provider as provider_module
    from app.services import ai_service

    error_type = getattr(provider_module, exception_name)

    class FailingProvider:
        provider = "arts"
        model = "deepseek-v4-flash"

        async def chat(self, *_args, **_kwargs):
            raise error_type("diagnostic detail")

    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: FailingProvider())
    monkeypatch.setattr(
        ai_service.project_context_service,
        "assemble_project_context",
        lambda **_: {"text": "", "profile": "", "chars": [], "budget": {}, "blocks": []},
    )

    result = await ai_service.chat_reply(_request("请分析当前人物关系"))

    assert result["mode"] == "failed"
    assert result["error_code"] == error_code
    assert message_fragment in result["reply"]
    assert result["retryable"] is retryable
