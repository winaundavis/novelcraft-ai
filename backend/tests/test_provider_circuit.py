"""P2-2 / P1-1（Codex 四、五轮）：LLM 超时按角色拆分 + 连续超时熔断。

覆盖：
- 连续超时达到阈值后，_real_chat 快速失败（熔断打开，走调用方 fallback）
- 成功后熔断清零恢复
- 按角色传递不同 timeout（writing=600s，短任务=90s）
- P1-1：熔断按**实际生效 provider**（角色路由后）检查与记录：
  - 激活 Kimi、角色路由 DeepSeek：DeepSeek 熔断 → 该角色被拦；Kimi 熔断 → 不误伤 DeepSeek
  - 流式 [DONE] 后成功复位（流式链路"超时→成功→再超时"不触发熔断）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.llm.provider import LLMResponseError, llm
from app.llm.roles import ROLE_CHECK, ROLE_PLANNING, ROLE_WRITING
from app.llm.provider import ROLE_TIMEOUT_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS


def _active_config(**overrides) -> dict:
    config = {
        "id": "cfg-1",
        "provider": "kimi",
        "apiKey": "sk-kimi",
        "apiBase": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "temperature": 0.8,
        "maxTokens": 4096,
        "roleOverrides": {},
        "isActive": True,
        "createdAt": "2026-08-10",
        "updatedAt": "2026-08-10",
    }
    config.update(overrides)
    return config


def _configure(config_overrides: dict | None = None, role_overrides: dict | None = None):
    """patch 激活配置并强制刷新 llm 实例运行时配置。"""
    cfg = _active_config(**(config_overrides or {}))
    if role_overrides is not None:
        cfg["roleOverrides"] = role_overrides

    class _Ctx:
        def __enter__(self):
            self._patcher = patch("app.llm.provider.get_active_llm_config", return_value=cfg)
            self._patcher.start()
            llm._refresh_runtime_config(force=True)
            return self

        def __exit__(self, *exc):
            self._patcher.stop()
            return False

    return _Ctx()


@pytest.fixture(autouse=True)
def _reset_circuit():
    """每个用例前重置熔断状态。"""
    llm._circuit = {}
    yield
    llm._circuit = {}


def test_circuit_breaker_opens_after_consecutive_timeouts():
    """连续超时 2 次（CIRCUIT_BREAK_THRESHOLD）→ 熔断打开。"""
    llm._record_circuit_timeout("kimi")
    assert not llm._circuit_open("kimi"), "1 次超时不应熔断"
    llm._record_circuit_timeout("kimi")
    assert llm._circuit_open("kimi"), "连续 2 次超时应熔断"
    # 冷却期内保持打开
    assert llm._circuit["kimi"]["open_until"] > 0


def test_circuit_breaker_success_resets_counter():
    """请求成功后熔断计数清零（不累积历史超时）。"""
    llm._record_circuit_timeout("kimi")
    llm._record_circuit_success("kimi")
    assert not llm._circuit_open("kimi")
    assert "kimi" not in llm._circuit


@pytest.mark.asyncio
async def test_circuit_breaker_fast_fail_in_real_chat():
    """熔断打开期间 _real_chat 快速抛错（不等上游 HTTP 超时）。"""
    with patch.object(llm, "_refresh_runtime_config"):
        llm.provider = "kimi"
        llm._record_circuit_timeout("kimi")
        llm._record_circuit_timeout("kimi")
        with pytest.raises(LLMResponseError, match="熔断"):
            await llm._real_chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_role_timeout_writing_uses_long_timeout():
    """正文生成（writing）角色使用长超时（600s），传给底层 _call_openai。"""
    mock_call = AsyncMock(return_value="ok")
    with _configure(), patch.object(llm, "assert_configured"), patch.object(llm, "_call_openai", new=mock_call):
        await llm.chat([{"role": "user", "content": "写一段正文"}], role=ROLE_WRITING)
    call = mock_call.await_args
    assert call.kwargs.get("timeout") == ROLE_TIMEOUT_SECONDS.get(ROLE_WRITING, DEFAULT_REQUEST_TIMEOUT_SECONDS)
    assert call.kwargs.get("timeout") == 600.0


@pytest.mark.asyncio
async def test_role_timeout_short_task_uses_default():
    """短任务（check）角色使用默认短超时（90s）。"""
    mock_call = AsyncMock(return_value="ok")
    with _configure(), patch.object(llm, "assert_configured"), patch.object(llm, "_call_openai", new=mock_call):
        await llm.chat([{"role": "user", "content": "核查一下"}], role=ROLE_CHECK)
    call = mock_call.await_args
    assert call.kwargs.get("timeout") == ROLE_TIMEOUT_SECONDS.get(ROLE_CHECK, DEFAULT_REQUEST_TIMEOUT_SECONDS)
    assert call.kwargs.get("timeout") == 90.0


@pytest.mark.asyncio
async def test_role_timeout_planning_allows_reasoning_to_finish():
    """Planning with thinking enabled must not inherit the 90s short-task timeout."""
    mock_call = AsyncMock(return_value="ok")
    with _configure(), patch.object(llm, "assert_configured"), patch.object(llm, "_call_openai", new=mock_call):
        await llm.chat([{"role": "user", "content": "优化大纲"}], role=ROLE_PLANNING)
    assert mock_call.await_args.kwargs.get("timeout") == 180.0


@pytest.mark.asyncio
async def test_explicit_timeout_kwarg_overrides_role():
    """显式传入 timeout kwargs 优先于角色配置。"""
    mock_call = AsyncMock(return_value="ok")
    with _configure(), patch.object(llm, "assert_configured"), patch.object(llm, "_call_openai", new=mock_call):
        await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK, timeout=300.0)
    assert mock_call.await_args.kwargs.get("timeout") == 300.0


# ══════════════════════════════════════════════════════
# P1-1：熔断按**实际生效 provider**（角色路由后）检查/记录
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_circuit_breaker_blocks_role_routed_provider_when_routed_is_open():
    """激活 Kimi、角色路由 DeepSeek：DeepSeek 已熔断 → 该角色应快速失败（不被误放行）。"""
    with _configure(
        config_overrides={"provider": "kimi", "model": "kimi-k2.6", "apiKey": "sk-kimi"},
        role_overrides={"check": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds"}},
    ):
        llm._record_circuit_timeout("deepseek")
        llm._record_circuit_timeout("deepseek")
        with pytest.raises(LLMResponseError, match="deepseek"):
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)


@pytest.mark.asyncio
async def test_circuit_breaker_active_provider_open_does_not_block_routed_provider():
    """激活 Kimi 已熔断、角色路由 DeepSeek：DeepSeek 健康 → 不应被误伤（真实调用发生）。"""
    with _configure(
        config_overrides={"provider": "kimi", "model": "kimi-k2.6", "apiKey": "sk-kimi"},
        role_overrides={"check": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds"}},
    ):
        llm._record_circuit_timeout("kimi")
        llm._record_circuit_timeout("kimi")
        with patch.object(llm, "_call_with_provider", new=AsyncMock(return_value="ok")) as mock_cwp:
            result = await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert result == "ok", "激活 provider 熔断不应阻止角色路由的健康 provider"
        assert mock_cwp.called, "角色路由的 provider 应真实调用"
        # 熔断记录用的 circuit_provider 应为 deepseek（实际生效），而非激活的 kimi
        assert mock_cwp.await_args.kwargs.get("circuit_provider") == "deepseek"


@pytest.mark.asyncio
async def test_circuit_breaker_records_routed_provider_on_call_success():
    """角色路由到 DeepSeek 成功 → 熔断按 DeepSeek 记录/复位（而非激活的 Kimi）。"""
    with _configure(
        config_overrides={"provider": "kimi", "model": "kimi-k2.6", "apiKey": "sk-kimi"},
        role_overrides={"check": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds"}},
    ):
        # 底层 _call_with_provider 内部调用 _call_openai（记录 circuit provider）
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_openai:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert mock_openai.await_args.kwargs.get("provider") == "deepseek"
        # 成功应复位 deepseek 的熔断（deepseek 不在熔断表）
        assert not llm._circuit_open("deepseek")
        assert not llm._circuit_open("kimi")


@pytest.mark.asyncio
async def test_stream_openai_resets_circuit_on_done():
    """流式 [DONE] 后熔断成功复位：超时→成功→再次超时 不触发熔断（Codex P1-1）。"""

    class _FakeResp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"你好"}}]}'
            yield "data: [DONE]"

    class _FakeStreamCtx:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *args, **kwargs):
            return _FakeStreamCtx()

    with _configure(config_overrides={"provider": "kimi", "model": "kimi-k2.6", "apiKey": "sk-kimi"}):
        # 1) 第一次超时：计数 1，未熔断
        llm._record_circuit_timeout("kimi")
        assert not llm._circuit_open("kimi")
        # 2) 流式成功（[DONE]）→ 清零
        with patch("app.llm.provider.httpx.AsyncClient", _FakeClient):
            chunks = []
            async for chunk in llm._stream_openai(
                [{"role": "user", "content": "hi"}], "kimi-k2.6", 1.0, 4096, provider="kimi"
            ):
                chunks.append(chunk)
        assert "".join(chunks) == "你好"
        assert not llm._circuit_open("kimi"), "流式 [DONE] 后熔断应复位"
        # 3) 再次超时：计数回到 1，仍不熔断（成功真正清零）
        llm._record_circuit_timeout("kimi")
        assert not llm._circuit_open("kimi"), "成功清零后单次超时不应触发熔断"
