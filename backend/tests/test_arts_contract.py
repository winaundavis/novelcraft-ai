# -*- coding: utf-8 -*-
"""[CD] 复测：ARTS 请求体契约测试——非流式/流式都必须携带推理关闭双保险。

背景：ARTS 中转 deepseek-v4-flash 默认开启推理，推理内容耗尽 max_tokens 预算
导致正文为空（finish_reason=length）。此前只在 provider.py 加了 reasoning_effort=none，
但无自动化契约测试，且长跑审计仍出现 7 次 reasoning>0（个别调用未生效）。

修复：改为双保险 reasoning_effort=none + thinking:{type:disabled}，任一被中转忽略时
另一仍生效。本测试用 FakeClient 捕获实际发出的 JSON body，断言：
- provider=arts 时，_call_openai 与 _stream_openai 的请求体都含 reasoning_effort=none 且 thinking.disabled
- 非 arts provider（kimi/deepseek）不附加该参数（不影响其他厂商）
- 捕获的流式请求含 stream:true
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.llm import audit as audit_mod
from app.llm.provider import LLMProvider


class _CapturingClient:
    """捕获请求 body 的假 AsyncClient。"""

    def __init__(self, response_json=None):
        self.captured_bodies: list[dict] = []
        self._response_json = response_json or {
            "choices": [{"message": {"content": "正文内容"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        body = kwargs.get("json") or kwargs.get("data") or {}
        if isinstance(body, str):
            body = json.loads(body)
        self.captured_bodies.append(body)
        return _JsonResponse(self._response_json)

    def stream(self, method, url, **kwargs):
        # httpx.AsyncClient.stream() 返回 context manager（非 coroutine）
        body = kwargs.get("json") or kwargs.get("data") or {}
        if isinstance(body, str):
            body = json.loads(body)
        self.captured_bodies.append(body)
        return _StreamContextManager(_StreamResponse())


class _JsonResponse:
    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.headers = {"x-request-id": "req-test"}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _StreamContextManager:
    """httpx.AsyncClient.stream() 返回的 context manager 包装。"""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _StreamResponse:
    def __init__(self):
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        # 一个内容块 + DONE
        chunk = json.dumps({"choices": [{"delta": {"content": "正文"}, "finish_reason": None}]})
        yield f"data: {chunk}"
        yield "data: [DONE]"


def _make_provider(provider: str) -> LLMProvider:
    p = LLMProvider(provider=provider, model="m", api_key="sk-test", api_base="https://x/v1")
    p._openai_key = "sk-test"
    p._openai_base = "https://x/v1"
    p._circuit = {}
    return p


def _capture(provider: LLMProvider, capturing: _CapturingClient):
    return patch("httpx.AsyncClient", return_value=capturing)


@pytest.fixture(autouse=True)
def _clean_audit_ctx():
    audit_mod.clear_llm_audit_context()
    yield
    audit_mod.clear_llm_audit_context()


@pytest.mark.asyncio
async def test_arts_nonstream_body_has_reasoning_none():
    """非流式：provider=arts 时请求体必须含推理关闭双保险。"""
    p = _make_provider("arts")
    cap = _CapturingClient()
    with _capture(p, cap):
        await p._call_openai(
            [{"role": "user", "content": "写正文"}], model="deepseek-v4-flash",
            temperature=0.8, max_tokens=4096, timeout=30,
        )
    assert cap.captured_bodies, "未捕获到请求体"
    body = cap.captured_bodies[0]
    assert body["reasoning_effort"] == "none", f"ARTS 非流式应传 reasoning_effort=none，实际: {body}"
    assert body["thinking"] == {"type": "disabled"}, f"ARTS 非流式应传 thinking.disabled，实际: {body}"
    assert body["model"] == "deepseek-v4-flash"
    assert body.get("stream") is not True


@pytest.mark.asyncio
async def test_arts_stream_body_has_reasoning_none_and_stream():
    """流式：provider=arts 时请求体必须含推理关闭双保险且 stream:true。"""
    p = _make_provider("arts")
    cap = _CapturingClient()
    chunks = []
    with _capture(p, cap):
        async for chunk in p._stream_openai(
            [{"role": "user", "content": "写正文"}], model="deepseek-v4-flash",
            temperature=0.8, max_tokens=4096, timeout=30,
        ):
            chunks.append(chunk)
    assert cap.captured_bodies, "未捕获到流式请求体"
    body = cap.captured_bodies[0]
    assert body["reasoning_effort"] == "none", f"ARTS 流式应传 reasoning_effort=none，实际: {body}"
    assert body["thinking"] == {"type": "disabled"}, f"ARTS 流式应传 thinking.disabled，实际: {body}"
    assert body["stream"] is True
    assert "".join(chunks) == "正文"


@pytest.mark.asyncio
async def test_non_arts_provider_not_affected():
    """非 arts provider（kimi/deepseek）不应附加 reasoning_effort=none。"""
    for provider in ("kimi", "deepseek"):
        p = _make_provider(provider)
        cap = _CapturingClient()
        with _capture(p, cap):
            await p._call_openai(
                [{"role": "user", "content": "hi"}], model="m",
                temperature=0.8, max_tokens=4096, timeout=30,
            )
        assert cap.captured_bodies
        body = cap.captured_bodies[0]
        assert "reasoning_effort" not in body, f"{provider} 不应有 reasoning_effort，实际: {body}"
