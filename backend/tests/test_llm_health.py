"""Health snapshots are free; only explicit, authorized POSTs probe a provider."""

import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import settings as settings_api
from app.core.config import settings
from app.core.terms import TERMS_VERSION
from app.dao import users
from app.llm.provider import LLMConfigurationError, LLMResponseError
from main import app


ORIGIN = "https://app.example.test"
HOSTILE_ORIGIN = "https://attacker.example.test"
HEALTH_PATH = "/api/settings/llm-health"
SAFE_ERROR = "模型连接检查失败，请稍后重试。"


class _ProbeProvider:
    provider = "synthetic-provider"
    model = "synthetic-model"
    configured = True
    error = None

    def __init__(self):
        self.calls = []

    def _refresh_runtime_config(self, force=False):
        pass

    def is_configured(self):
        return self.configured

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return "连接正常"


@pytest.fixture
def probe_provider(monkeypatch, live_llm_call_spy):
    provider = _ProbeProvider()
    monkeypatch.setattr(settings_api, "llm", provider)
    yield provider
    assert live_llm_call_spy() == 0


@pytest.fixture
def production_client(monkeypatch):
    for name, value in {
        "environment": "production", "debug": False,
        "auth_cookie_secure": True, "cors_allow_origins": ORIGIN,
        "admin_user_ids": "", "invite_tokens": "", "rate_limit": 10000,
    }.items():
        monkeypatch.setattr(settings, name, value)
    for middleware in app.user_middleware:
        if middleware.cls is TrustedHostMiddleware:
            monkeypatch.setitem(middleware.kwargs, "allowed_hosts", ["*"])
    monkeypatch.setattr(app, "middleware_stack", None)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def _login(client, *, admin=True):
    username = "health-" + uuid.uuid4().hex
    users.create_user(username, "strong-pass-1", is_admin=admin)
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "strong-pass-1", "termsAccepted": True, "termsVersion": TERMS_VERSION},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("headers", [
    {},
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Referer": HOSTILE_ORIGIN + "/"},
])
def test_get_returns_unchecked_snapshot_without_provider_call(production_client, probe_provider, headers):
    _login(production_client)
    response = production_client.get(HEALTH_PATH, headers=headers)
    assert response.status_code == 200
    assert probe_provider.calls == []
    assert response.json()["data"] == {
        "configured": True, "provider": "synthetic-provider", "model": "synthetic-model",
        "checked": False, "ok": False, "error": "",
    }


def test_cross_site_post_is_rejected_without_provider_call(production_client, probe_provider):
    _login(production_client)
    response = production_client.post(
        HEALTH_PATH, headers={"Origin": HOSTILE_ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert probe_provider.calls == []


def test_cookie_post_without_origin_is_rejected_without_provider_call(production_client, probe_provider):
    _login(production_client)
    assert production_client.post(HEALTH_PATH).status_code == 403
    assert probe_provider.calls == []


def test_trusted_admin_post_probes_once_with_small_output_limit(production_client, probe_provider):
    _login(production_client)
    response = production_client.post(HEALTH_PATH, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json()["data"]["checked"] is True
    assert response.json()["data"]["ok"] is True
    assert len(probe_provider.calls) == 1
    _, kwargs = probe_provider.calls[0]
    assert 0 < kwargs["max_tokens"] <= 32
    assert kwargs["thinking"] == "disabled"


def test_ordinary_user_cannot_post_probe(production_client, probe_provider):
    _login(production_client, admin=False)
    assert production_client.post(HEALTH_PATH, headers={"Origin": ORIGIN}).status_code == 403
    assert probe_provider.calls == []


def test_unauthenticated_user_cannot_post_probe(production_client, probe_provider):
    assert production_client.post(HEALTH_PATH, headers={"Origin": ORIGIN}).status_code == 401
    assert probe_provider.calls == []


@pytest.mark.parametrize("error_type", [RuntimeError, LLMConfigurationError, LLMResponseError])
def test_probe_errors_are_fixed_and_never_expose_exception_details(production_client, probe_provider, error_type):
    _login(production_client)
    private_detail = "synthetic-secret-key https://private.invalid/token synthetic-private-content"
    probe_provider.error = error_type(private_detail)
    response = production_client.post(HEALTH_PATH, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert len(probe_provider.calls) == 1
    data = response.json()["data"]
    assert data["checked"] is True
    assert data["ok"] is False
    assert data["error"] == SAFE_ERROR
    assert private_detail not in response.text
    assert "synthetic-secret-key" not in response.text
    assert "private.invalid" not in response.text


@pytest.mark.parametrize("method,checked", [("GET", False), ("POST", True)])
def test_unconfigured_snapshot_and_probe_do_not_call_provider(production_client, probe_provider, method, checked):
    _login(production_client)
    probe_provider.configured = False
    response = production_client.request(method, HEALTH_PATH, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["configured"] is False
    assert data["checked"] is checked
    assert data["ok"] is False
    assert data["error"] == "当前没有可用的模型配置。"
    assert probe_provider.calls == []
