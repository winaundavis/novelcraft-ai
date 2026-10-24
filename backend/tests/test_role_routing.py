"""模型档位路由层（建议4：自检分级 L0/L1/L2）单元测试。

覆盖：
- chat(role=...) 角色路由：不同角色用不同 model 调 LLM（mock 底层通道捕获参数）
- 未配置角色 / 未知角色回退激活配置
- chat_for 便捷方法等价于 chat(role=...)
- DB role_overrides 覆盖优先于代码默认档位
- 显式 kwargs 优先于角色档位
- 角色指定不同 provider 且 key 可用时切换通道
- 角色配置异常（role_overrides 非法类型 / 未知角色）不抛异常，回退激活配置
- chat_stream 角色路由

LLM 底层通道全部 mock，不发起真实网络请求。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.llm.provider import llm
from app.llm.roles import (
    ALL_ROLES,
    DEFAULT_ROLE_PROFILES,
    ROLE_CHECK,
    ROLE_FULLTEXT,
    ROLE_LABELS,
    ROLE_REASON,
    ROLE_STYLE,
    ROLE_SUMMARY,
    ROLE_UNSPECIFIED,
    ROLE_WRITING,
)


def _active_config(**overrides) -> dict:
    config = {
        "id": "cfg-1",
        "provider": "openai",
        "apiKey": "sk-test",
        "apiBase": "https://api.openai.com/v1",
        "model": "gpt-4o",
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
    """patch 激活配置并强制刷新 llm 实例的运行时配置。返回 context 管理器。"""
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


def _model_of(mock_call) -> str:
    """从 _call_openai/_call_anthropic 的 mock 调用中提取 model（位置参数 1）。"""
    return mock_call.call_args.args[1]


def _temperature_of(mock_call) -> float:
    """从底层通道 mock 调用中提取 temperature（位置参数 2）。"""
    return mock_call.call_args.args[2]


def _model_of_each(mock_call) -> list[str]:
    return [call.args[1] for call in mock_call.call_args_list]


# ══════════════════════════════════════════════════════
# 角色常量与默认档位
# ══════════════════════════════════════════════════════

def test_roles_constants_complete():
    formal_roles = {
        ROLE_WRITING, ROLE_SUMMARY, ROLE_CHECK, ROLE_REASON, ROLE_STYLE, ROLE_FULLTEXT,
    }
    assert formal_roles <= set(ALL_ROLES)
    # P2：unspecified 是缺失 role 的兜底标记，不进入审校路由（无 LABEL/默认档位）
    assert ROLE_UNSPECIFIED in ALL_ROLES
    for role in formal_roles:
        assert ROLE_LABELS[role]
        assert role in DEFAULT_ROLE_PROFILES
    assert ROLE_UNSPECIFIED not in ROLE_LABELS
    assert ROLE_UNSPECIFIED not in DEFAULT_ROLE_PROFILES


def test_default_role_profiles_temperature():
    # 确定性自检温度最低，正文生成不强制覆盖温度
    assert DEFAULT_ROLE_PROFILES[ROLE_CHECK]["temperature"] == 0.1
    assert DEFAULT_ROLE_PROFILES[ROLE_SUMMARY]["temperature"] == 0.3
    assert DEFAULT_ROLE_PROFILES[ROLE_REASON]["temperature"] == 0.2
    assert DEFAULT_ROLE_PROFILES[ROLE_STYLE]["temperature"] == 0.3


# ══════════════════════════════════════════════════════
# chat(role=...) 角色路由：不同角色用不同 model
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chat_role_uses_role_override_model():
    """check 角色配置了 deepseek-flash → 该角色调用用 deepseek-flash，正文仍用激活配置。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert _model_of(mock_call) == "deepseek-flash"


@pytest.mark.asyncio
async def test_chat_without_role_uses_unspecified_not_check():
    """P2（Codex 复测）：不传角色且审计上下文无 role → 记录为 unspecified（走激活配置），
    不再静默归为 check——避免遗漏 role 的写作请求被错误路由到审校模型。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}])
        # unspecified → 激活配置（gpt-4o），而非审校模型 deepseek-flash
        assert _model_of(mock_call) == "gpt-4o"


@pytest.mark.asyncio
async def test_chat_unknown_role_falls_back_to_active():
    """未知角色 → 回退激活配置，不抛异常。"""
    with _configure():
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role="unknown-role")
        assert _model_of(mock_call) == "gpt-4o"


@pytest.mark.asyncio
async def test_different_roles_use_different_models():
    """不同角色调用 LLM 使用不同 model（mock：捕获每次调用参数）。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash"}, "summary": {"model": "deepseek-chat"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "a"}], role=ROLE_CHECK)
            await llm.chat([{"role": "user", "content": "b"}], role=ROLE_SUMMARY)
            await llm.chat([{"role": "user", "content": "c"}], role=ROLE_WRITING)
        assert _model_of_each(mock_call) == ["deepseek-flash", "deepseek-chat", "gpt-4o"]


# ══════════════════════════════════════════════════════
# chat_for 便捷方法
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chat_for_equals_chat_with_role():
    with _configure(role_overrides={"reason": {"model": "gpt-4.1"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat_for(ROLE_REASON, [{"role": "user", "content": "hi"}])
        assert _model_of(mock_call) == "gpt-4.1"


# ══════════════════════════════════════════════════════
# 覆盖优先级：显式 kwargs > DB 覆盖 > 默认档位 > 激活配置
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_explicit_kwargs_override_role():
    """调用点显式传入的 model 优先于角色档位。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK, model="explicit-model")
        assert _model_of(mock_call) == "explicit-model"


@pytest.mark.asyncio
async def test_role_default_temperature_applied():
    """角色未配置温度时用代码默认档位（check=0.1），覆盖激活配置的 0.8。"""
    with _configure():
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert _temperature_of(mock_call) == 0.1


@pytest.mark.asyncio
async def test_role_override_temperature_wins_over_default():
    """DB role_overrides 的温度优先于代码默认档位。"""
    with _configure(role_overrides={"check": {"temperature": 0.5}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert _temperature_of(mock_call) == 0.5


@pytest.mark.asyncio
async def test_explicit_temperature_kwarg_wins():
    with _configure():
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK, temperature=0.9)
        assert _temperature_of(mock_call) == 0.9


@pytest.mark.asyncio
async def test_writing_role_keeps_active_temperature():
    """正文生成角色默认不覆盖温度 → 沿用激活配置温度。"""
    with _configure():
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_WRITING)
        assert _temperature_of(mock_call) == 0.8


# ══════════════════════════════════════════════════════
# 角色 provider 切换
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_role_provider_switch_uses_that_channel():
    """角色指定不同 provider（claude）且有 key → 走 Anthropic 通道。"""
    with _configure(role_overrides={"check": {"provider": "claude", "model": "claude-3-5-sonnet"}}):
        with patch.object(llm, "_call_anthropic", new=AsyncMock(return_value="ok")) as mock_anthropic:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert _model_of(mock_anthropic) == "claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_role_provider_without_key_falls_back():
    """角色指定不同 provider 但无 key → 回退当前通道（激活配置 model）。"""
    with _configure(role_overrides={"check": {"provider": "claude", "model": "claude-3-5-sonnet"}}):
        # 清空 provider key：让 _provider_api_key 返回空 → 回退
        with patch.object(llm, "_provider_api_key", return_value=""):
            with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
                await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
            # 回退到当前 provider：model 仍用角色档位覆盖的 model（通道用当前 key）
            assert _model_of(mock_call) == "claude-3-5-sonnet"


# ══════════════════════════════════════════════════════
# 异常容错
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_invalid_role_overrides_type_ignored():
    """role_overrides 非 dict（脏数据）→ 不抛异常，回退激活配置。"""
    with _configure(role_overrides="not-a-dict"):  # type: ignore[arg-type]
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert _model_of(mock_call) == "gpt-4o"


@pytest.mark.asyncio
async def test_role_config_not_dict_ignored():
    """单个角色的覆盖值不是 dict → 忽略该角色覆盖。"""
    with _configure(role_overrides={"check": "oops"}):  # type: ignore[dict-item]
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        assert _model_of(mock_call) == "gpt-4o"


# ══════════════════════════════════════════════════════
# chat_stream 角色路由
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chat_stream_role_routing():
    """流式调用同样按角色路由 model。"""
    with _configure(role_overrides={"style": {"model": "gpt-4o-mini"}}):
        with patch.object(llm, "_stream_openai", new=_FakeStream()):
            chunks = []
            async for chunk in llm.chat_stream([{"role": "user", "content": "hi"}], role=ROLE_STYLE):
                chunks.append(chunk)
            assert "".join(chunks) == "ok"
            # 2026-10-19：所有角色的 thinking 统一改为 disabled ——
            # 推理会吃光 max_tokens（审计 babfeffb67b1 / 701654cc2014 两次事故）。
            # 流式与非流式必须同样遵循，否则又会出现"某条路径偷偷开推理"。
            assert llm._stream_openai.request_policy == {
                "thinking": {"type": "disabled"},
                "reasoning_effort": "none",
            }


class _FakeStream:
    """模拟流式通道：async generator 方法。"""
    async def __call__(self, messages, model, temperature, max_tokens, timeout=None, provider=None, request_policy=None):
        self.model = model
        self.request_policy = request_policy
        yield "ok"


# ══════════════════════════════════════════════════════
# 第 2 档：角色独立 provider + 独立 API key / api_base
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_role_independent_api_key_uses_dedicated_channel():
    """角色配了独立 api_key → 走 _call_with_provider，独立 key/base 透传。"""
    with _configure(role_overrides={
        "check": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-role-check",
            "api_base": "https://custom.example.com/v1",
        },
    }):
        with patch.object(llm, "_call_with_provider", new=AsyncMock(return_value="ok")) as mock_cwp:
            await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        call = mock_cwp.await_args
        assert call.args[0] == "openai"
        assert call.args[1] == "sk-role-check"
        assert call.args[3] == "gpt-4o-mini"
        assert call.kwargs["api_base"] == "https://custom.example.com/v1"


@pytest.mark.asyncio
async def test_two_roles_independent_keys_used_respectively():
    """两个不同 provider 的角色各自配置独立 key → 各自 key 被分别使用（真正跨厂商）。"""
    role_overrides = {
        "check": {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds-role"},
        "summary": {"provider": "claude", "model": "claude-haiku", "api_key": "sk-ant-role"},
    }
    with _configure(role_overrides=role_overrides):
        with patch.object(llm, "_call_with_provider", new=AsyncMock(return_value="ok")) as mock_cwp:
            await llm.chat([{"role": "user", "content": "a"}], role=ROLE_CHECK)
            await llm.chat([{"role": "user", "content": "b"}], role=ROLE_SUMMARY)
        keys = [call.args[1] for call in mock_cwp.await_args_list]
        providers = [call.args[0] for call in mock_cwp.await_args_list]
        assert providers == ["deepseek", "claude"]
        assert keys == ["sk-ds-role", "sk-ant-role"]


@pytest.mark.asyncio
async def test_role_without_independent_key_falls_back_to_global():
    """角色未配独立 api_key → 回退 _provider_api_key(provider)（向后兼容）。"""
    with _configure(role_overrides={"check": {"provider": "claude", "model": "claude-3-5-sonnet"}}):
        with patch.object(llm, "_provider_api_key", return_value="sk-global-ant") as mock_key:
            with patch.object(llm, "_call_with_provider", new=AsyncMock(return_value="ok")) as mock_cwp:
                await llm.chat([{"role": "user", "content": "hi"}], role=ROLE_CHECK)
        # P1-1：effective_provider 解析（熔断）与角色路由各查一次 key，共 2 次
        mock_key.assert_any_call("claude")
        assert mock_cwp.await_args.args[1] == "sk-global-ant"


# ══════════════════════════════════════════════════════
# 第 3 档：长度路由（long_model 切换）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_long_model_switched_when_input_exceeds_threshold():
    """角色配置 long_model 且输入超阈值（60K）→ 切换长窗模型。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash", "long_model": "deepseek-long"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "长" * 61000}], role=ROLE_CHECK)
        assert _model_of(mock_call) == "deepseek-long"


@pytest.mark.asyncio
async def test_long_model_not_used_below_threshold():
    """未超阈值 → 用角色档位模型（不切换）。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash", "long_model": "deepseek-long"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "短"}], role=ROLE_CHECK)
        assert _model_of(mock_call) == "deepseek-flash"


@pytest.mark.asyncio
async def test_long_model_disabled_by_default():
    """未配置 long_model → 输入超阈值也不切换（默认不启用）。"""
    with _configure(role_overrides={"check": {"model": "deepseek-flash"}}):
        with patch.object(llm, "_call_openai", new=AsyncMock(return_value="ok")) as mock_call:
            await llm.chat([{"role": "user", "content": "长" * 61000}], role=ROLE_CHECK)
        assert _model_of(mock_call) == "deepseek-flash"
