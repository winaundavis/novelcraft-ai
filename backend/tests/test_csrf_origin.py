"""Production origin checks use real HTTP middleware and real session cookies."""

import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.auth import COOKIE
from app.core.config import settings
from app.core.terms import TERMS_VERSION
from app.dao import projects, users
from main import app


TRUSTED_ORIGIN = "https://app.example.test"
SECOND_ORIGIN = "https://admin.example.test"
HOSTILE_ORIGIN = "https://attacker.example.test"


@pytest.fixture
def production_client(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "rate_limit", 10000)
    monkeypatch.setattr(settings, "cors_allow_origins", f"{TRUSTED_ORIGIN}, {SECOND_ORIGIN}")
    # CORS/host middleware normally capture deployment settings at app import.
    # Rebuild only this test's stack so preflight exercises the same allowlist.
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            monkeypatch.setitem(middleware.kwargs, "allow_origins", [TRUSTED_ORIGIN, SECOND_ORIGIN])
        elif middleware.cls is TrustedHostMiddleware:
            monkeypatch.setitem(middleware.kwargs, "allowed_hosts", ["*"])
    monkeypatch.setattr(app, "middleware_stack", None)
    with TestClient(app, base_url=TRUSTED_ORIGIN) as client:
        yield client


def _login(client):
    username = "csrf-" + uuid.uuid4().hex
    user_id = users.create_user(username, "strong-pass-1")
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "strong-pass-1", "termsAccepted": True, "termsVersion": TERMS_VERSION},
        headers={"Origin": TRUSTED_ORIGIN},
    )
    assert response.status_code == 200
    return user_id, response


def test_trusted_login_sets_secure_session_cookie(production_client):
    user_id, response = _login(production_client)

    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert response.headers["access-control-allow-origin"] == TRUSTED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert production_client.get("/api/auth/me").json()["data"]["id"] == user_id


def test_hostile_logout_preserves_session_and_trusted_logout_revokes_it(production_client):
    user_id, _ = _login(production_client)
    token = production_client.cookies.get(COOKIE)

    hostile = production_client.post("/api/auth/logout", headers={"Origin": HOSTILE_ORIGIN})
    assert hostile.status_code == 403
    assert "set-cookie" not in hostile.headers
    assert users.user_for_session(token)["id"] == user_id
    assert production_client.get("/api/auth/me").status_code == 200

    trusted = production_client.post("/api/auth/logout", headers={"Origin": TRUSTED_ORIGIN})
    assert trusted.status_code == 200
    assert users.user_for_session(token) is None
    assert production_client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("fetch_site", [None, "same-origin", "same-site"])
def test_session_cookie_without_origin_is_rejected(production_client, fetch_site):
    user_id, _ = _login(production_client)
    token = production_client.cookies.get(COOKIE)
    headers = {"Sec-Fetch-Site": fetch_site} if fetch_site else {}

    assert production_client.post("/api/auth/logout", headers=headers).status_code == 403
    assert users.user_for_session(token)["id"] == user_id


def test_empty_session_cookie_still_requires_origin(production_client):
    production_client.cookies.set(COOKIE, "")
    assert production_client.post("/api/auth/logout").status_code == 403


@pytest.mark.parametrize("origin", [
    HOSTILE_ORIGIN,
    "null",
    "",
    TRUSTED_ORIGIN + "/",
    TRUSTED_ORIGIN + ".attacker.test",
    "http://app.example.test",
    "https://app.example.test:444",
    TRUSTED_ORIGIN + " " + HOSTILE_ORIGIN,
])
def test_origin_must_match_allowlist_exactly_even_without_cookie(production_client, origin):
    response = production_client.post("/api/auth/logout", headers={"Origin": origin})
    assert response.status_code == 403


def test_null_origin_is_rejected_even_if_misconfigured_in_allowlist(production_client, monkeypatch):
    monkeypatch.setattr(settings, "cors_allow_origins", f"{TRUSTED_ORIGIN},null")
    assert production_client.post("/api/auth/logout", headers={"Origin": "null"}).status_code == 403


def test_duplicate_origin_headers_are_rejected(production_client):
    response = production_client.post(
        "/api/auth/logout", headers=[("Origin", TRUSTED_ORIGIN), ("Origin", HOSTILE_ORIGIN)]
    )
    assert response.status_code == 403


@pytest.mark.parametrize("environment", ["production", "prod", "beta"])
def test_explicit_production_guard_is_independent_of_debug(production_client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "debug", True)
    assert production_client.post("/api/auth/logout", headers={"Origin": HOSTILE_ORIGIN}).status_code == 403


@pytest.mark.parametrize("environment", ["development", "test"])
def test_nonproduction_debug_false_keeps_existing_behavior(production_client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    production_client.cookies.set(COOKIE, "synthetic-invalid-session")
    assert production_client.post("/api/auth/logout").status_code == 200
    assert production_client.post("/api/auth/logout", headers={"Origin": HOSTILE_ORIGIN}).status_code == 200


def test_no_cookie_no_origin_preserves_header_client_mutations(production_client, monkeypatch):
    user_id = users.create_user("csrf-header-" + uuid.uuid4().hex, "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"synthetic-header-invite:{user_id}")

    response = production_client.post(
        "/api/projects/", json={"name": "header-client-project"},
        headers={"X-NovelCraft-Invite": "synthetic-header-invite"},
    )
    assert response.status_code == 200
    assert projects.get_project_owner(response.json()["data"]["id"]) == user_id
    assert COOKIE not in production_client.cookies


def test_cross_site_fetch_requires_trusted_origin_without_cookie(production_client):
    headers = {"Sec-Fetch-Site": "cross-site"}
    assert production_client.post("/api/auth/logout", headers=headers).status_code == 403
    headers["Origin"] = SECOND_ORIGIN
    assert production_client.post("/api/auth/logout", headers=headers).status_code == 200


@pytest.mark.parametrize("method,expected", [("GET", 200), ("HEAD", 405), ("OPTIONS", 405)])
def test_safe_methods_bypass_guard_with_session_cookie(production_client, method, expected):
    production_client.cookies.set(COOKIE, "synthetic-invalid-session")
    response = production_client.request(method, "/api/health", headers={"Origin": "null"})
    assert response.status_code == expected


def test_preflight_uses_cors_allowlist_and_does_not_require_session_origin(production_client):
    production_client.cookies.set(COOKIE, "synthetic-invalid-session")
    headers = {"Origin": SECOND_ORIGIN, "Access-Control-Request-Method": "POST"}
    trusted = production_client.options("/api/auth/logout", headers=headers)
    assert trusted.status_code == 200
    assert trusted.headers["access-control-allow-origin"] == SECOND_ORIGIN
    assert trusted.headers["access-control-allow-credentials"] == "true"
    assert "POST" in trusted.headers["access-control-allow-methods"]

    headers["Origin"] = HOSTILE_ORIGIN
    hostile = production_client.options("/api/auth/logout", headers=headers)
    assert hostile.status_code == 400
    assert "access-control-allow-origin" not in hostile.headers


@pytest.mark.parametrize("origin", [None, HOSTILE_ORIGIN])
def test_forwarded_and_referer_headers_cannot_bypass_guard(production_client, origin):
    production_client.cookies.set(COOKIE, "synthetic-invalid-session")
    headers = {
        "Forwarded": "for=127.0.0.1;host=attacker.example.test;proto=https",
        "X-Forwarded-Host": "attacker.example.test",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "127.0.0.1",
        "Referer": TRUSTED_ORIGIN + "/account",
    }
    if origin is not None:
        headers["Origin"] = origin
    assert production_client.post("/api/auth/logout", headers=headers).status_code == 403


@pytest.mark.parametrize("path,first_status", [
    ("/api/auth/login", 401), ("/api/auth/register", 403), ("/api/auth/logout", 200),
])
def test_rejections_do_not_consume_auth_or_mutation_rate_limits(production_client, monkeypatch, path, first_status):
    monkeypatch.setattr(settings, "rate_limit", 1)
    for _ in range(2):
        denied = production_client.post(path, json={}, headers={"Origin": HOSTILE_ORIGIN})
        assert denied.status_code == 403
    first = production_client.post(path, json={}, headers={"Origin": TRUSTED_ORIGIN})
    assert first.status_code == first_status
    second = production_client.post(path, json={}, headers={"Origin": TRUSTED_ORIGIN})
    assert second.status_code == 429


def test_origin_guard_runs_before_project_ownership(production_client):
    response = production_client.delete("/api/projects/nonexistent", headers={"Origin": HOSTILE_ORIGIN})
    assert response.status_code == 403
