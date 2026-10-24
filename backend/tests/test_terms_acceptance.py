"""Explicit agreement binds registration/login to the version actually displayed."""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core.config import settings
from app.core.terms import TERMS_VERSION
from app.dao import invites as invites_dao
from app.dao import users as users_dao
from app.db import get_conn
from main import app


def _mint_invite() -> str:
    return invites_dao.generate_invites(1, "pytest-terms", "pytest")[0]


@pytest.fixture(autouse=True)
def _relax(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "rate_limit", 10000)


def _name() -> str:
    return "terms-" + uuid.uuid4().hex[:16]


def _agreement() -> dict:
    return {"termsAccepted": True, "termsVersion": TERMS_VERSION}


def _credentials(username: str) -> dict:
    return {"username": username, "password": "strong-pass-1"}


def _assert_no_session(user_id: str) -> None:
    with get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()[0] == 0


def _assert_pending_invite(code: str) -> None:
    invite = invites_dao.find_by_code(code)
    assert invite["status"] == "pending"
    assert invite["used_count"] == 0


def test_register_without_accepting_terms_is_rejected():
    username, code = _name(), _mint_invite()
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register", json={**_credentials(username), "invite": code}
        )
    assert response.status_code == 400, response.text
    assert "用户协议" in response.json()["detail"]
    assert "set-cookie" not in response.headers
    assert users_dao.find_user(username) is None
    _assert_pending_invite(code)


def test_register_records_terms_version_and_time():
    username = _name()
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            json={**_credentials(username), "invite": _mint_invite(), **_agreement()},
        )
        assert response.status_code == 200, response.text
        assert client.get("/api/auth/me").json()["data"]["username"] == username
    user = users_dao.find_user(username)
    assert user["terms_version"] == TERMS_VERSION
    assert user["terms_accepted_at"]


@pytest.mark.parametrize("agreement", [{}, {"termsAccepted": False, "termsVersion": TERMS_VERSION}])
def test_login_without_explicit_acceptance_does_not_create_session(agreement):
    username = _name()
    uid = users_dao.create_user(username, "strong-pass-1")
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={**_credentials(username), **agreement})
    assert response.status_code == 400, response.text
    assert "用户协议" in response.json()["detail"]
    assert "set-cookie" not in response.headers
    assert users_dao.get_user(uid)["terms_version"] == ""
    _assert_no_session(uid)


@pytest.mark.parametrize("version", ["", "2026-10-17"])
def test_register_missing_or_stale_version_does_not_consume_invite(version):
    username, code = _name(), _mint_invite()
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            json={**_credentials(username), "invite": code, "termsAccepted": True, "termsVersion": version},
        )
    assert response.status_code == 409, response.text
    assert "重新阅读" in response.json()["detail"]
    assert "set-cookie" not in response.headers
    assert users_dao.find_user(username) is None
    _assert_pending_invite(code)


@pytest.mark.parametrize("version", ["", "2026-10-17"])
def test_login_missing_or_stale_version_preserves_previous_acceptance(version):
    username = _name()
    uid = users_dao.create_user(username, "strong-pass-1")
    users_dao.record_terms_acceptance(uid, "previous-version")
    before = users_dao.get_user(uid)
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={**_credentials(username), "termsAccepted": True, "termsVersion": version},
        )
    assert response.status_code == 409, response.text
    assert "重新阅读" in response.json()["detail"]
    assert "set-cookie" not in response.headers
    after = users_dao.get_user(uid)
    assert after["terms_version"] == before["terms_version"]
    assert after["terms_accepted_at"] == before["terms_accepted_at"]
    _assert_no_session(uid)


@pytest.mark.parametrize("accepted", ["yes", 1])
@pytest.mark.parametrize("endpoint", ["register", "login"])
def test_agreement_requires_a_json_boolean(endpoint, accepted):
    username, code = _name(), _mint_invite()
    uid = users_dao.create_user(username, "strong-pass-1") if endpoint == "login" else None
    with TestClient(app) as client:
        response = client.post(
            f"/api/auth/{endpoint}",
            json={**_credentials(username), "invite": code, "termsAccepted": accepted, "termsVersion": TERMS_VERSION},
        )
    assert response.status_code == 422, response.text
    assert "set-cookie" not in response.headers
    _assert_pending_invite(code)
    if uid:
        assert users_dao.get_user(uid)["terms_version"] == ""
        _assert_no_session(uid)
    else:
        assert users_dao.find_user(username) is None


def test_registration_terms_storage_failure_leaves_no_account_or_consumed_invite():
    username, code = _name(), _mint_invite()
    # Both write shapes fail: the old INSERT-then-UPDATE exposes its orphaned
    # account, while the corrected atomic INSERT rolls back without using a code.
    with TestClient(app, raise_server_exceptions=False) as client:
        with get_conn() as conn:
            conn.execute(
                "CREATE TRIGGER reject_test_terms_insert BEFORE INSERT ON users "
                "WHEN NEW.username = '" + username + "' AND NEW.terms_version != '' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic terms write failure'); END"
            )
            conn.execute(
                "CREATE TRIGGER reject_test_terms_update BEFORE UPDATE OF terms_version ON users "
                "WHEN NEW.username = '" + username + "' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic terms write failure'); END"
            )
        try:
            response = client.post(
                "/api/auth/register",
                json={**_credentials(username), "invite": code, **_agreement()},
            )
        finally:
            with get_conn() as conn:
                conn.execute("DROP TRIGGER reject_test_terms_insert")
                conn.execute("DROP TRIGGER reject_test_terms_update")
    assert response.status_code == 503, response.text
    assert "暂时不可用" in response.json()["detail"]
    assert "用户名" not in response.text
    assert "synthetic terms write failure" not in response.text
    assert "set-cookie" not in response.headers
    assert users_dao.find_user(username) is None
    _assert_pending_invite(code)


def test_old_users_migration_does_not_fabricate_acceptance_and_login_can_record_it(monkeypatch, tmp_path):
    isolated_db = tmp_path / "old-users.sqlite3"
    username = _name()
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
            "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, 0, ?)",
            ("old-terms-user", username, users_dao.hash_password("strong-pass-1"), "2026-10-01T00:00:00+00:00"),
        )
    monkeypatch.setattr(db, "DB_PATH", isolated_db)
    db.init_db()
    db.init_db()  # Migrations must also be safe after a service restart.
    migrated = users_dao.get_user("old-terms-user")
    assert migrated["terms_version"] == ""
    assert migrated["terms_accepted_at"] == ""
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={**_credentials(username), **_agreement()})
        assert response.status_code == 200, response.text
        assert client.get("/api/auth/me").json()["data"]["id"] == "old-terms-user"
    stored = users_dao.get_user("old-terms-user")
    assert stored["terms_version"] == TERMS_VERSION
    assert stored["terms_accepted_at"]


def test_wrong_password_still_returns_authentication_error_before_terms_check():
    username = _name()
    uid = users_dao.create_user(username, "strong-pass-1")
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": username, "password": "wrong-pass"})
    assert response.status_code == 401, response.text
    assert "set-cookie" not in response.headers
    assert users_dao.get_user(uid)["terms_version"] == ""
    _assert_no_session(uid)


def test_invite_login_requires_agreement_before_session_and_records_current_version():
    username, code = _name(), _mint_invite()
    uid = users_dao.create_user(username, "strong-pass-1")
    assert invites_dao.bind_invite(invites_dao.hash_code(code), uid)
    with TestClient(app) as client:
        rejected = client.post("/api/auth/login", json={"invite": code})
        assert rejected.status_code == 400, rejected.text
        assert "set-cookie" not in rejected.headers
        _assert_no_session(uid)
        assert users_dao.get_user(uid)["terms_version"] == ""
        accepted = client.post(
            "/api/auth/login",
            json={"invite": code, **_agreement()},
        )
        assert accepted.status_code == 200, accepted.text
    assert users_dao.get_user(uid)["terms_version"] == TERMS_VERSION
