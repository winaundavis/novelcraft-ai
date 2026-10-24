# -*- coding: utf-8 -*-
"""Codex 八轮：LLM 响应脱敏元数据 + 固定响应夹具测试。

覆盖（对应 DeepSeek 任务「固定响应夹具」）：
- content="" 且无 reasoning → LLMResponseError("空内容") + 审计 error 记录元数据
- content="" 且 finish_reason=length / 存在 reasoning_content → 报错消息含"推理内容可能耗尽"
- content 非空 + reasoning_content → success + 审计记录 reasoning_chars / finish_reason / usage
- 审计表落库请求侧元数据（message_count/input_chars/est_tokens/max_tokens/temperature）
"""

from __future__ import annotations

import sqlite3
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.llm import audit as audit_mod
from app.llm.provider import LLMProvider, LLMResponseError


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._resp = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        return self._resp


@pytest.fixture(autouse=True)
def _clean_audit_ctx():
    audit_mod.clear_llm_audit_context()
    yield
    audit_mod.clear_llm_audit_context()


@pytest.fixture()
def audit_db(tmp_path, monkeypatch):
    db_path = tmp_path / "meta_audit.db"
    monkeypatch.setattr(audit_mod, "DB_PATH", db_path)
    return db_path


def _make_provider() -> LLMProvider:
    p = LLMProvider(provider="kimi", model="kimi-k2.6", api_key="sk-test", api_base="https://api.moonshot.cn/v1")
    p._openai_key = "sk-test"
    p._openai_base = "https://api.moonshot.cn/v1"
    p._circuit = {}
    return p


def _patch_client(provider: LLMProvider, response: _FakeResponse):
    fake = _FakeClient(response)
    return patch("httpx.AsyncClient", return_value=fake)


def _rows(db_path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM llm_audit_log ORDER BY created_at DESC").fetchall()]
    con.close()
    return rows


async def _run_openai(provider, response):
    with _patch_client(provider, response):
        return await provider._call_openai(
            [{"role": "user", "content": "写一段正文"}],
            model="kimi-k2.6", temperature=1.0, max_tokens=4096, timeout=30,
        )


@pytest.mark.asyncio
async def test_empty_content_with_reasoning_length_raises_and_meta(audit_db):
    """content='' + finish_reason=length + 长 reasoning → 报错且消息含"推理内容可能耗尽"，审计 error 带元数据。"""
    provider = _make_provider()
    resp = _FakeResponse(200, {
        "choices": [{
            "message": {"content": "", "reasoning_content": "x" * 1200},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 4096, "total_tokens": 5596},
    }, headers={"x-moonshot-request-id": "req-abc123"})
    with pytest.raises(LLMResponseError, match="推理内容可能耗尽"):
        await _run_openai(provider, resp)

    rows = _rows(audit_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "error"
    assert row["finish_reason"] == "length"
    assert row["reasoning_chars"] == 1200
    assert row["completion_tokens"] == 4096
    assert row["prompt_tokens"] == 1500
    assert row["total_tokens"] == 5596
    assert row["request_id"] == "req-abc123"
    # 请求侧元数据
    assert row["message_count"] == 1
    assert row["max_tokens"] == 4096
    assert row["temperature"] == 1.0
    assert row["input_chars"] > 0
    assert row["est_tokens"] == row["input_chars"]


@pytest.mark.asyncio
async def test_plain_empty_content_raises(audit_db):
    """content='' 且无 reasoning → 报错"空内容"，审计 error。"""
    provider = _make_provider()
    resp = _FakeResponse(200, {
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    })
    with pytest.raises(LLMResponseError, match="空内容"):
        await _run_openai(provider, resp)
    rows = _rows(audit_db)
    assert rows[0]["status"] == "error"
    assert rows[0]["output_chars"] == 0


@pytest.mark.asyncio
async def test_content_with_reasoning_success_meta(audit_db):
    """content 非空 + reasoning_content → success，审计记录 reasoning_chars / finish_reason / usage。"""
    provider = _make_provider()
    resp = _FakeResponse(200, {
        "choices": [{
            "message": {"content": "她推开那扇沉重的木门。", "reasoning_content": "r" * 300},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 500, "total_tokens": 1700},
    }, headers={"x-moonshot-request-id": "req-xyz"})
    out = await _run_openai(provider, resp)
    assert out == "她推开那扇沉重的木门。"
    rows = _rows(audit_db)
    row = rows[0]
    assert row["status"] == "success"
    assert row["finish_reason"] == "stop"
    assert row["reasoning_chars"] == 300
    assert row["completion_tokens"] == 500
    assert row["total_tokens"] == 1700
    assert row["request_id"] == "req-xyz"
    assert row["output_chars"] == len("她推开那扇沉重的木门。")


@pytest.mark.asyncio
async def test_reasoning_without_length_but_empty_still_error(audit_db):
    """有 reasoning 但 content 空且 finish_reason=stop → 仍报错（推理存在但正文未生成也算失败）。"""
    provider = _make_provider()
    resp = _FakeResponse(200, {
        "choices": [{"message": {"content": "", "reasoning_content": "y" * 50}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 100, "total_tokens": 110},
    })
    with pytest.raises(LLMResponseError):
        await _run_openai(provider, resp)
    rows = _rows(audit_db)
    assert rows[0]["status"] == "error"
    assert rows[0]["reasoning_chars"] == 50


@pytest.mark.asyncio
async def test_http_error_records_meta(audit_db):
    """HTTP 非 2xx → 审计 error + http_status / request_id。"""
    provider = _make_provider()
    # raise_for_status 在 200 下不抛；这里模拟 500：直接构造 status_code=500 且 raise_for_status 抛错
    class _ErrResponse(_FakeResponse):
        def raise_for_status(self):
            raise __import__("httpx").HTTPStatusError("500", request=None, response=None)
    resp = _ErrResponse(500, {"detail": "boom"}, headers={"x-request-id": "req-500"})
    with _patch_client(provider, resp):
        with pytest.raises(LLMResponseError):
            await provider._call_openai(
                [{"role": "user", "content": "hi"}], model="kimi-k2.6", temperature=1.0,
                max_tokens=4096, timeout=30,
            )
    rows = _rows(audit_db)
    assert rows[0]["status"] == "error"
    assert rows[0]["http_status"] == 500
    assert rows[0]["request_id"] == "req-500"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("httpx_error", "expected_exception"),
    [
        ("ConnectError", "LLMTransportError"),
        ("ReadTimeout", "LLMTimeoutError"),
    ],
)
async def test_openai_transport_failures_have_distinct_error_types(
    audit_db, httpx_error, expected_exception
):
    """Transport and timeout failures must remain distinguishable above the provider layer."""
    import httpx
    from app.llm import provider as provider_module

    provider = _make_provider()

    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", "https://api.example.test/chat/completions")
            raise getattr(httpx, httpx_error)("network failure", request=request)

    with patch("httpx.AsyncClient", return_value=_RaisingClient()):
        with pytest.raises(getattr(provider_module, expected_exception)):
            await provider._call_openai(
                [{"role": "user", "content": "hi"}],
                model="kimi-k2.6",
                temperature=1.0,
                max_tokens=100,
                timeout=30,
            )


@pytest.mark.asyncio
async def test_successful_call_settles_reserved_budget_to_actual_provider_usage(monkeypatch):
    """A successful 10-token response must not permanently consume the 102-token reservation."""
    from app.core.config import settings
    from app.db import get_conn, utc_today
    from app.llm import audit as audit_module

    project_id = "budget-settlement-" + uuid.uuid4().hex
    provider = _make_provider()
    response = _FakeResponse(
        200,
        {
            "choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
    )
    monkeypatch.setattr(settings, "project_daily_llm_call_limit", 30)
    monkeypatch.setattr(settings, "project_daily_token_limit", 1000)
    token = audit_module.set_llm_audit_context(project_id=project_id, role="planning", merge=False)
    try:
        with _patch_client(provider, response):
            result = await provider._real_chat(
                [{"role": "user", "content": "hi"}],
                role="planning",
                max_tokens=100,
            )
        assert result == "完成"
        with get_conn() as conn:
            row = conn.execute(
                "SELECT calls, tokens FROM project_llm_usage WHERE project_id = ? AND usage_date = ?",
                (project_id, utc_today()),
            ).fetchone()
        assert dict(row) == {"calls": 1, "tokens": 10}
    finally:
        audit_module.reset_llm_audit_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,method_name", [
    ("deepseek", "_call_deepseek"),
    ("claude", "_call_anthropic"),
])
async def test_non_native_stream_fallback_reserves_budget_once(monkeypatch, provider_name, method_name):
    """One synthetic streamed request must consume one call, even via the non-stream fallback."""
    provider = _make_provider()
    provider.provider = provider_name
    provider._openai_key = ""
    provider._deepseek_key = "test-key" if provider_name == "deepseek" else ""
    provider._anthropic_key = "test-key" if provider_name == "claude" else ""
    monkeypatch.setattr(provider, "_refresh_runtime_config", lambda: None)
    monkeypatch.setattr(provider, "_resolve_role_profile", lambda role: {})
    dispatch = AsyncMock(return_value="synthetic response")
    monkeypatch.setattr(provider, method_name, dispatch)
    reserve = Mock()
    monkeypatch.setattr("app.services.project_budget_service.consume_project_budget", reserve)

    result = "".join([chunk async for chunk in provider._real_chat_stream(
        [{"role": "user", "content": "hi"}], role="writing", max_tokens=100,
    )])

    assert result == "synthetic response"
    dispatch.assert_awaited_once()
    reserve.assert_called_once_with(102)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["openai", "stream", "anthropic"])
@pytest.mark.parametrize("failure_name,expected_exception", [
    ("timeout", "LLMTimeoutError"),
    ("transport", "LLMTransportError"),
    ("upstream", "LLMUpstreamError"),
])
async def test_provider_failure_text_does_not_expose_upstream_url(entrypoint, failure_name, expected_exception):
    """Job/SSE clients may receive str(error), so upstream paths and queries must stay private."""
    import httpx
    from app.llm import provider as provider_module

    provider = _make_provider()
    secret = "synthetic-private-url-token"
    request = httpx.Request("POST", f"https://provider.example.test/{secret}?api_key={secret}")
    if failure_name == "timeout":
        failure = httpx.ReadTimeout(f"Timeout at {request.url}", request=request)
    elif failure_name == "transport":
        failure = httpx.ConnectError(f"Connection failed at {request.url}", request=request)
    else:
        response = httpx.Response(403, request=request)
        failure = httpx.HTTPStatusError(f"Rejected at {request.url}", request=request, response=response)

    class _RaisingStream:
        async def __aenter__(self):
            raise failure

        async def __aexit__(self, *exc):
            return False

    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise failure

        def stream(self, *args, **kwargs):
            return _RaisingStream()

    with patch("httpx.AsyncClient", return_value=_RaisingClient()):
        with pytest.raises(getattr(provider_module, expected_exception)) as raised:
            if entrypoint == "stream":
                _ = [chunk async for chunk in provider._stream_openai([], "test-model", 1.0, 100)]
            else:
                method = provider._call_openai if entrypoint == "openai" else provider._call_anthropic
                await method([], "test-model", 1.0, 100)

    assert secret not in str(raised.value)
    assert "provider.example.test" not in str(raised.value)
