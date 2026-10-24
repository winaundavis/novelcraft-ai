"""Phase 10-F: deterministic model and generation-completion contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.llm import audit as audit_mod
from app.llm.provider import LLMProvider, LLMReasoningBudgetExhaustedError
from app.llm.roles import ROLE_COPILOT, ROLE_PLANNING, ROLE_WRITING
from app.models.schemas import ChatRequest, ContinueWritingRequest
from app.services import ai_service
from app.services import beta_smoke_manifest_service
from app.services import novel_generation_service as generation_service


class _Response:
    status_code = 200
    headers = {"x-request-id": "phase10f-request"}

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": "可提交的测试正文。"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }


class _Client:
    def __init__(self):
        self.bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, _url, **kwargs):
        self.bodies.append(kwargs["json"])
        return _Response()


def _provider() -> LLMProvider:
    provider = LLMProvider(
        provider="deepseek", model="deepseek-v4-flash", api_key="test-key",
    )
    provider._deepseek_key = "test-key"
    provider._circuit = {}
    return provider


def _arts_provider() -> LLMProvider:
    provider = LLMProvider(
        provider="arts", model="deepseek-v4-flash", api_key="test-key", api_base="https://example.invalid/v1",
    )
    provider._openai_key = "test-key"
    provider._circuit = {}
    return provider


@pytest.mark.asyncio
async def test_prose_generation_disables_thinking_in_final_request(monkeypatch):
    """正文角色必须显式关闭 thinking，不能依赖 provider 默认值。"""
    client = _Client()
    provider = _provider()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", lambda _tokens: None)
    with patch("httpx.AsyncClient", return_value=client):
        await provider._real_chat([{"role": "user", "content": "写正文"}], role=ROLE_WRITING)

    assert client.bodies[0]["thinking"] == {"type": "disabled"}
    assert client.bodies[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_plot_planning_disables_thinking_to_protect_the_output_budget(monkeypatch):
    """规划角色必须显式**关闭** thinking（2026-10-19 修正）。

    背景：`b180bcb` 曾把规划角色改成 `thinking=enabled`，并用下面这条用例钉住。
    但在 arts + deepseek-v4-flash + max_tokens=4096 下，推理把输出预算全部吃掉：
    审计 `babfeffb67b1` 记录 `finish_reason=length`、`output_chars=0`、耗时 58s，
    大纲因此**稳定**降级成本地模板（接口仍 200，用户只看到"AI 服务暂时不可用"）。

    大纲是结构化 JSON 输出，用不着推理；而推理长度不可控，给它留预算等于
    拿核心功能赌模型当次思考多长。若确实想开，请用 `llm_configs.role_overrides`
    显式给 planning 打开并同时调高 max_tokens，不要改这里的默认值。
    """
    client = _Client()
    provider = _provider()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", lambda _tokens: None)
    with patch("httpx.AsyncClient", return_value=client):
        await provider._real_chat([{"role": "user", "content": "规划章节"}], role=ROLE_PLANNING)

    assert client.bodies[0]["thinking"] == {"type": "disabled"}
    assert client.bodies[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_arts_respects_role_thinking_in_final_request(monkeypatch):
    planning_client = _Client()
    writing_client = _Client()
    provider = _arts_provider()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", lambda _tokens: None)

    with patch("httpx.AsyncClient", return_value=planning_client):
        await provider._real_chat([{"role": "user", "content": "规划大纲"}], role=ROLE_PLANNING)
    with patch("httpx.AsyncClient", return_value=writing_client):
        await provider._real_chat([{"role": "user", "content": "写正文"}], role=ROLE_WRITING)

    # ARTS 上规划角色同样必须关闭推理：否则推理吃掉 max_tokens，正文 0 字
    # （审计 babfeffb67b1：finish_reason=length / output_chars=0）。
    assert planning_client.bodies[0]["thinking"] == {"type": "disabled"}
    assert planning_client.bodies[0]["reasoning_effort"] == "none"
    assert writing_client.bodies[0]["thinking"] == {"type": "disabled"}
    assert writing_client.bodies[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_copilot_chat_uses_non_thinking_role(monkeypatch):
    """Copilot 对话不能误用正文角色，从而意外开启错误的任务策略。"""
    monkeypatch.setattr(
        ai_service.project_context_service,
        "assemble_project_context",
        lambda **_kwargs: {"text": "上下文", "profile": "copilot", "chars": 3, "budget": 100, "blocks": []},
    )
    chat = AsyncMock(return_value="候选修改")
    monkeypatch.setattr(ai_service.llm, "chat", chat)

    await ai_service.chat_reply(
        ChatRequest(projectId="p1", chapterId="c1", messages=[{"role": "user", "content": "改写这句"}])
    )

    assert chat.await_args.kwargs["role"] == ROLE_COPILOT


@pytest.mark.asyncio
async def test_apply_suggestion_uses_non_thinking_copilot_role(monkeypatch):
    """旧建议应用路径也属于局部编辑，必须保持 Copilot 策略。"""
    chat = AsyncMock(return_value="候选修改")
    monkeypatch.setattr(ai_service.llm, "chat", chat)

    await ai_service.apply_suggestion_to_text("s1", "modify", "正文", "原文")

    assert chat.await_args.kwargs["role"] == ROLE_COPILOT


@pytest.mark.asyncio
async def test_streaming_length_limit_emits_incomplete_status(monkeypatch):
    """流式续写若被截断，客户端必须得到不可提交状态而非普通完成。"""
    async def stream(*_args, **_kwargs):
        yield "未完成片段"

    monkeypatch.setattr(ai_service.llm, "chat_stream", stream)
    monkeypatch.setattr(
        ai_service.project_context_service,
        "assemble_project_context",
        lambda **_kwargs: {"text": "上下文"},
    )
    monkeypatch.setattr(
        ai_service,
        "get_llm_call_meta",
        lambda: {"finish_reason": "length"},
    )

    events = [event async for event in ai_service.continue_writing_stream(ContinueWritingRequest(projectId="p1", chapterId="c1"))]

    assert any('"generation_incomplete": true' in event for event in events)


@pytest.mark.asyncio
async def test_provider_stream_records_length_finish_reason(monkeypatch):
    """流式 provider 必须把最终 length 原因写入调用元数据。"""
    class Response:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"片段"},"finish_reason":"length"}]}'
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return False

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return StreamContext()

    provider = _provider()
    monkeypatch.setattr("app.llm.provider.httpx.AsyncClient", Client)
    chunks = [
        chunk async for chunk in provider._stream_openai(
            [{"role": "user", "content": "写正文"}], "deepseek-v4-flash", 0.8, 4096, provider="deepseek"
        )
    ]

    assert "".join(chunks) == "片段"
    assert audit_mod.get_llm_call_meta()["finish_reason"] == "length"


def test_beta_default_model_and_generation_token_cap_are_safe():
    assert settings.default_llm_provider == "deepseek"
    assert settings.default_llm_model == "deepseek-v4-flash"
    assert settings.default_max_tokens == 4096
    assert settings.default_max_tokens != 700


def test_controlled_single_chapter_budget_allows_only_one_generation_call():
    """受控单章模式不能隐式扩大为重试、QA、改写或补写链。"""
    job = generation_service.NovelGenerationJob(
        id="job", project_id="p1", mode="single", requested_chapter_id="c1",
        status="queued", message="", target_words=2000, total_chapters=1,
        max_retries=0,
    )

    budget = generation_service._chapter_budget_for_job(job, target_words=2000)

    assert budget.max_generation_calls == 1
    assert budget.max_retry_calls == 0
    assert budget.max_qa_calls == 0
    assert budget.max_rewrite_calls == 0


def test_controlled_single_chapter_discards_client_retry_request():
    """服务端不能只依赖前端把单章重试值传成 0。"""
    assert generation_service._effective_max_retries("single", 2) == 0
    assert generation_service._effective_max_retries("all", 2) == generation_service.MAX_RETRY_CALLS


@pytest.mark.asyncio
async def test_controlled_single_chapter_skips_post_generation_checks(monkeypatch):
    """受控单章已拿到正文后不得额外调用 review 或改写链。"""
    job = generation_service.NovelGenerationJob(
        id="job", project_id="p1", mode="single", requested_chapter_id="c1",
        status="running", message="", target_words=2000, total_chapters=1,
        max_retries=0,
    )
    evaluate = AsyncMock()
    logic = AsyncMock()
    language = AsyncMock()
    monkeypatch.setattr(generation_service.deterministic_qa_service, "evaluate_chapter", evaluate)
    monkeypatch.setattr(generation_service.review_service, "review_logic", logic)
    monkeypatch.setattr(generation_service.review_service, "review_language", language)

    result = await generation_service._run_self_check_loop(
        job=job,
        chapter={"id": "c1", "title": "第一章", "order": 1},
        result={"content": "可提交正文" * 400, "chapter_contract": {"version": 1}},
        memory_context=None,
        previous_id=None,
        next_id=None,
        hook_instruction="",
    )

    assert result["llm_qa_calls"] == 0
    assert result["rewrite_count"] == 0
    evaluate.assert_not_called()
    logic.assert_not_awaited()
    language.assert_not_awaited()


def test_job_row_preserves_an_explicit_zero_retry_limit():
    """重启恢复任务时，0 不能被 Python 的 truthiness 默认为 1。"""
    job = generation_service._row_to_job({
        "id": "job", "project_id": "p1", "mode": "single", "requested_chapter_id": "c1",
        "status": "queued", "message": "", "target_words": 2000, "total_chapters": 1,
        "completed_chapters": 0, "failed_chapters": 0, "fallback_chapters": 0,
        "generated_words": 0, "current_chapter_id": None, "current_chapter_title": "",
        "last_completed_chapter_id": None, "last_completed_chapter_title": "",
        "llm_provider": "", "llm_model": "", "last_generation_source": "",
        "started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "finished_at": None, "error": "", "cancel_requested": 0, "max_retries": 0,
        "retry_count": 0, "created_at": "2026-01-01T00:00:00Z", "checkpoint_type": "",
        "resume_target_json": "", "pending_manual_recovery": 0, "heartbeat_at": "",
        "fact_check_count": 0, "rewrite_count": 0, "hook_miss_count": 0,
        "quality_debt_count": 0, "escalation_count": 0,
    })

    assert job.max_retries == 0


def test_prose_manifest_exposes_effective_model_policy_and_source():
    manifest = _provider().request_manifest(ROLE_WRITING)
    assert manifest["provider"] == "deepseek"
    assert manifest["model"] == "deepseek-v4-flash"
    assert manifest["thinking"] == {"type": "disabled"}
    assert manifest["reasoning_effort"] == "none"
    # 2026-10-20：正文角色多了 8192 的输出**下限**（全局默认 4096 对长章节不够，
    # 截断的正文会被当成成功结果静默落库）。manifest 必须反映真实请求值。
    assert manifest["max_tokens"] == 8192
    assert manifest["config_source"] == "explicit provider configuration"


def test_role_output_floor_never_lowers_a_higher_global_budget():
    """角色值只抬下限：作者把全局 max_tokens 调到 16384 时，不能被 8192 砍回去。"""
    provider = _provider()
    assert provider._resolve_max_tokens({"max_tokens": 8192}, {}) == max(8192, provider.max_tokens)
    # 非正文角色不受影响
    assert provider._resolve_max_tokens({}, {}) == provider.max_tokens
    # 显式传值（例如大纲按章节数算出的预算）优先于一切
    assert provider._resolve_max_tokens({"max_tokens": 8192}, {"max_tokens": 14000}) == 14000
    assert provider._resolve_max_tokens({"max_tokens": 8192}, {"max_tokens": 2000}) == 2000


@pytest.mark.asyncio
async def test_length_limited_partial_continuation_is_marked_incomplete(monkeypatch):
    """部分正文被 length 截断时不可以伪装为成功结果。"""
    monkeypatch.setattr(ai_service.llm, "chat", AsyncMock(return_value="未完成的正文"))
    monkeypatch.setattr(
        ai_service, "get_llm_call_meta",
        lambda: {"provider": "deepseek", "model": "deepseek-v4-flash", "audit_id": "a1", "finish_reason": "length"},
    )
    result = await ai_service.generate_continuation_result(
        ContinueWritingRequest(projectId="p1", chapterId="c1", maxAttempts=1)
    )
    assert result["source"] == "incomplete_output"
    assert result["generation_incomplete"] is True


@pytest.mark.asyncio
async def test_incomplete_generation_cannot_enter_commit_or_projection(monkeypatch):
    """生成不完整时必须在任何故事状态或提交写入之前拒绝。"""
    ensure_state = AsyncMock()
    monkeypatch.setattr(generation_service.story_state_service, "ensure_story_state", ensure_state)
    job = generation_service.NovelGenerationJob(
        id="job", project_id="p1", mode="single", requested_chapter_id="c1", status="running",
        message="", target_words=500, total_chapters=1,
    )
    with pytest.raises(generation_service.GenerationIncompleteError):
        await generation_service._persist_chapter_and_sync_memory(
            job=job,
            chapter={"id": "c1", "title": "第一章", "order": 1},
            result={"content": "未完成的正文", "generation_incomplete": True},
            outline={"nodes": []},
        )
    ensure_state.assert_not_called()


@pytest.mark.asyncio
async def test_empty_generation_cannot_enter_commit_or_projection(monkeypatch):
    """`finish_reason=length` 后没有正文时，空结果同样不能进入提交链。"""
    ensure_state = AsyncMock()
    monkeypatch.setattr(generation_service.story_state_service, "ensure_story_state", ensure_state)
    job = generation_service.NovelGenerationJob(
        id="job", project_id="p1", mode="single", requested_chapter_id="c1", status="running",
        message="", target_words=500, total_chapters=1,
    )
    with pytest.raises(generation_service.GenerationIncompleteError):
        await generation_service._persist_chapter_and_sync_memory(
            job=job,
            chapter={"id": "c1", "title": "第一章", "order": 1},
            result={"content": "", "generation_incomplete": True},
            outline={"nodes": []},
        )
    ensure_state.assert_not_called()


def test_dry_run_manifest_is_secret_free_and_binds_story_style_and_contract(monkeypatch):
    monkeypatch.setattr(beta_smoke_manifest_service.projects_dao, "get_project", lambda _id: {
        "id": "p1", "premise": "测试设定", "characters": [{"name": "林乔"}],
        "worldRules": [{"name": "盐灯"}], "styleConfig": {"name": "Emotional Resonance"},
    })
    monkeypatch.setattr(beta_smoke_manifest_service.chapters_dao, "get_chapter", lambda *_args: {"id": "c1", "title": "第一章"})
    monkeypatch.setattr(beta_smoke_manifest_service.chapter_contracts_dao, "get_latest_contract", lambda *_args: {"id": "contract-1", "version": 2, "goal": "封桥前过河"})
    monkeypatch.setattr(beta_smoke_manifest_service.llm, "request_manifest", lambda _role: {
        "provider": "deepseek", "model": "deepseek-v4-flash", "thinking": {"type": "disabled"},
        "reasoning_effort": "none", "max_tokens": 4096, "temperature": 0.8, "config_source": "settings default",
    })

    manifest = beta_smoke_manifest_service.build_manifest("p1", "c1")

    assert manifest["model"] == "deepseek-v4-flash"
    assert manifest["thinking"] == {"type": "disabled"}
    assert manifest["story_hash"]
    assert manifest["style_profile"]["name"] == "Emotional Resonance"
    assert manifest["chapter_contract"]["version"] == 2
    assert "api_key" not in str(manifest).lower()


# ── 推理耗尽输出预算（2026-10-19 事故的回归钉子）────────────────
#
# 事故：审计 `babfeffb67b1` —— planning 开着 reasoning，max_tokens=4096 全用在思考上，
# 正文 0 字、finish_reason=length，大纲静默降级成本地模板，
# 而前端显示的是"AI 服务暂时不可用"（把排查引向网络/密钥）。


class _ReasoningOnlyResponse:
    """有推理内容、但正文为空且被 max_tokens 截断 —— 正是事故现场。"""

    status_code = 200
    headers = {"x-request-id": "reasoning-only"}

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{
                "message": {"content": "", "reasoning_content": "先分析人物关系，再规划三卷结构……"},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 710, "completion_tokens": 4096, "total_tokens": 4806},
        }


class _SequenceClient:
    """按顺序返回多次响应，并记录每次请求体。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, _url, **kwargs):
        self.bodies.append(kwargs["json"])
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


@pytest.mark.asyncio
async def test_reasoning_exhausted_budget_gets_a_distinct_error_code(monkeypatch):
    """必须是 `output_budget_exhausted`，不能笼统归成 `response_invalid`。

    否则前端只能说"AI 服务暂时不可用"，用户和排查者都看不到真实原因。
    """
    client = _SequenceClient(_ReasoningOnlyResponse())
    provider = _arts_provider()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", lambda _tokens: None)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(LLMReasoningBudgetExhaustedError) as exc_info:
            await provider._real_chat(
                [{"role": "user", "content": "规划大纲"}], role=ROLE_PLANNING, thinking="enabled",
            )

    assert exc_info.value.error_code == "output_budget_exhausted"
    assert exc_info.value.reasoning_chars > 0
    assert exc_info.value.finish_reason == "length"


@pytest.mark.asyncio
async def test_reasoning_exhaustion_retries_once_with_thinking_disabled(monkeypatch):
    """第一次把预算烧在推理上 → 自动关推理重试一次，并把正文带回来。

    这是"不要静默降级"的核心：能用关推理救回来的，就不该直接给用户模板内容。
    """
    client = _SequenceClient(_ReasoningOnlyResponse(), _Response())
    provider = _arts_provider()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", lambda _tokens: None)

    with patch("httpx.AsyncClient", return_value=client):
        reply = await provider._real_chat(
            [{"role": "user", "content": "规划大纲"}], role=ROLE_PLANNING, thinking="enabled",
        )

    assert reply == "可提交的测试正文。"
    assert len(client.bodies) == 2, "必须只重试一次，不能无限重试"
    # 第一次：按用户/角色配置开推理
    assert client.bodies[0]["thinking"] == {"type": "enabled"}
    # 第二次：必须真的关掉推理（否则重试毫无意义）
    assert client.bodies[1]["thinking"] == {"type": "disabled"}
    assert client.bodies[1]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_plain_output_truncation_is_not_retried(monkeypatch):
    """没有推理、纯粹是输出预算不够 → 重试无益，不许白烧一次调用。"""

    class _PlainTruncated(_ReasoningOnlyResponse):
        def json(self):
            return {
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            }

    client = _SequenceClient(_PlainTruncated(), _Response())
    provider = _arts_provider()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", lambda _tokens: None)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(LLMReasoningBudgetExhaustedError):
            await provider._real_chat(
                [{"role": "user", "content": "规划大纲"}], role=ROLE_PLANNING, thinking="disabled",
            )

    assert len(client.bodies) == 1, "无推理的截断不应重试"
