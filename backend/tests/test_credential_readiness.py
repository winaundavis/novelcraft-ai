"""Readiness inspects at-rest credentials without contacting any provider."""

import asyncio
import json
import sqlite3
from contextlib import closing

from cryptography.fernet import Fernet
import pytest

from app.core.config import settings
from app.core.secret_box import PREFIX
from main import _readiness_state, readiness


@pytest.fixture
def credential_database(monkeypatch, tmp_path):
    path = tmp_path / "credentials.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE users (id TEXT, is_admin INTEGER)")
        conn.execute("INSERT INTO users VALUES ('synthetic-admin', 1)")
        conn.execute("CREATE TABLE llm_configs (api_key TEXT, role_overrides TEXT)")
    for name, value in {
        "environment": "production", "debug": False,
        "cors_allow_origins": "https://app.example.com", "allowed_hosts": "app.example.com",
        "auth_cookie_secure": True, "admin_user_ids": "synthetic-admin", "invite_tokens": "",
        "app_encryption_key": Fernet.generate_key().decode("ascii"),
        "data_dir": str(tmp_path), "novelcraft_db_path": str(path),
    }.items():
        monkeypatch.setattr(settings, name, value)
    return path


@pytest.mark.parametrize("kind", ["plaintext-main", "plaintext-role", "wrong-master", "invalid-roles"])
def test_readiness_rejects_unprotected_or_unreadable_credentials(credential_database, kind):
    stored, roles = "", "{}"
    if kind == "plaintext-main":
        stored = "synthetic-main-key"
    elif kind == "plaintext-role":
        roles = json.dumps({"writing": {"api_key": "synthetic-role-key"}})
    elif kind == "wrong-master":
        stored = PREFIX + Fernet(Fernet.generate_key()).encrypt(b"synthetic-key").decode()
    else:
        roles = '{"writing":'
    with closing(sqlite3.connect(credential_database)) as conn, conn:
        conn.execute("INSERT INTO llm_configs VALUES (?, ?)", (stored, roles))

    ready, errors = _readiness_state()
    assert ready is False
    assert "credentials_not_ready" in errors
    response = asyncio.run(readiness())
    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "not_ready", "version": "1.0.0"}


def test_readiness_accepts_encrypted_credentials_and_empty_fallbacks(credential_database):
    box = Fernet(settings.app_encryption_key.encode())
    stored = PREFIX + box.encrypt(b"synthetic-main-key").decode()
    roles = {"writing": {"api_key": PREFIX + box.encrypt(b"synthetic-role-key").decode()},
             "check": {"api_key": "", "model": "synthetic-model"}}
    with closing(sqlite3.connect(credential_database)) as conn, conn:
        conn.execute("INSERT INTO llm_configs VALUES (?, ?)", (stored, json.dumps(roles)))
        conn.execute("INSERT INTO llm_configs VALUES ('', '{}')")

    assert _readiness_state() == (True, [])
