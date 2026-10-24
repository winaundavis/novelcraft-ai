"""账号注册 · 邀请码绑定（PRD 第五节验收标准的后端可验证部分）。

覆盖验收 1–17；其中 14–17 只验证脚本的**纯函数**与**临时夹具库**，
绝不在真实库上执行迁移（真实库只读）。

测试库由 `tests/conftest.py` 全进程共用一个临时文件，因此本文件的用户名/邀请码
一律带递增后缀，避免与其它测试文件互相污染。
"""

from __future__ import annotations

import itertools
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.terms import TERMS_VERSION
from app import db as app_db
from app.dao import invites as invites_dao
from app.dao import users as users_dao
from app.db import get_conn
from app.services import invite_service
from main import app
from scripts import check_orphan_owners, migrate_legacy_owner

_COUNTER = itertools.count(1)


def _name(prefix: str) -> str:
    return f"{prefix}-{next(_COUNTER)}"


def _mint(count: int = 1, label: str = "pytest-invites") -> list[str]:
    """取库里的明文邀请码（只在此刻拿得到，之后库里只有哈希）。"""
    return invites_dao.generate_invites(count, label, "pytest")


def _register(client: TestClient, username: str, code: str, password: str = "strong-pass-1"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite": code, "termsAccepted": True, "termsVersion": TERMS_VERSION},
    )


def _login(client: TestClient, username: str, password: str = "strong-pass-1"):
    return client.post("/api/auth/login", json={"username": username, "password": password, "termsAccepted": True, "termsVersion": TERMS_VERSION})


def _login_with_invite(client: TestClient, code: str):
    return client.post("/api/auth/login", json={"username": "", "password": "", "invite": code, "termsAccepted": True, "termsVersion": TERMS_VERSION})


@pytest.fixture(autouse=True)
def _relax_auth_rate_limit(monkeypatch):
    """本文件认证请求较多，放宽限流，避免 429 干扰用例语义。"""
    monkeypatch.setattr(settings, "rate_limit", 100000)


# ── 注册（验收 1–6） ──────────────────────────────────────


def test_register_with_pending_invite_auto_logs_in_and_binds_code(monkeypatch):
    """验收 1：pending 码 + 合规表单 → 成功且**响应即带会话 cookie**，码变 bound。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    username = _name("reg")
    with TestClient(app) as client:
        response = _register(client, username, code)
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        assert payload["username"] == username
        assert "password" not in response.text and "hash" not in response.text
        # 自动登录（Q4）：注册响应本身就种了会话，不需要再登录一次
        assert "novelcraft_session" in response.cookies
        me = client.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["data"]["id"] == payload["id"]
    stored = invites_dao.find_by_code(code)
    assert stored["status"] == "bound"
    assert stored["bound_user_id"] == payload["id"]
    assert stored["bound_at"], "绑定时间必须留痕（Q1=C+留痕）"


def test_invite_is_single_use_and_second_registration_is_rejected(monkeypatch):
    """验收 2：同一个码第二个人注册必须 403，且第一个人仍然有效。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    first, second = _name("first"), _name("second")
    with TestClient(app) as client:
        assert _register(client, first, code).status_code == 200
        rejected = _register(client, second, code)
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == invite_service.REGISTER_ERRORS["used"][1]
        # 第一个人不受影响：会话还在，密码也能登录
        assert client.get("/api/auth/me").json()["data"]["username"] == first
        assert _login(client, first).status_code == 200
    assert users_dao.find_user(second) is None, "第二个人的账号不该被建出来"


def test_failed_registration_does_not_consume_pending_invite(monkeypatch):
    """验收 3：注册失败（用户名占用 / 密码不合规 / 用户名非法）都不消耗码。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    taken = _name("taken")
    with TestClient(app) as client:
        assert _register(client, taken, _mint()[0]).status_code == 200

        conflict = _register(client, taken, code)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == invite_service.USERNAME_TAKEN_MESSAGE
        assert invites_dao.find_by_code(code)["status"] == "pending"

        weak = _register(client, _name("weak"), code, password="1234567")
        assert weak.status_code == 400
        assert invites_dao.find_by_code(code)["status"] == "pending"

        illegal = _register(client, "bad name", code)
        assert illegal.status_code == 400
        assert invites_dao.find_by_code(code)["status"] == "pending"

        # 换一个干净的组合，码仍然有效（证明前面几次失败确实没把它用掉）
        assert _register(client, _name("fresh"), code).status_code == 200
    assert invites_dao.find_by_code(code)["status"] == "bound"


def test_disabled_invite_cannot_be_used_for_registration(monkeypatch):
    """验收 4：disabled 码注册 → 403「已被停用」。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    row = invites_dao.find_by_code(code)
    invites_dao.disable_invite(invite_id=row["id"])
    with TestClient(app) as client:
        response = _register(client, _name("disabled"), code)
        assert response.status_code == 403
        assert response.json()["detail"] == invite_service.REGISTER_ERRORS["disabled"][1]


def test_password_rules_reject_short_and_username_identical(monkeypatch):
    """验收 5：7 位密码 / 密码与用户名相同（含大小写变体）→ 400，绝不静默接受。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    codes = _mint(3)
    username = _name("pwrule")
    with TestClient(app) as client:
        short = _register(client, username, codes[0], password="1234567")
        assert short.status_code == 400
        assert short.json()["detail"] == invite_service.PASSWORD_RULE_MESSAGE

        identical = _register(client, username, codes[1], password=username)
        assert identical.status_code == 400
        assert identical.json()["detail"] == invite_service.PASSWORD_RULE_MESSAGE

        identical_upper = _register(client, username, codes[2], password=username.upper())
        assert identical_upper.status_code == 400, "密码与用户名比较必须不区分大小写"
    for code in codes:
        assert invites_dao.find_by_code(code)["status"] == "pending"


def test_empty_invite_tokens_configuration_no_longer_allows_open_registration(monkeypatch):
    """验收 6（Q6）：空 INVITE_TOKENS 时一律拒绝注册，不再"没配置就放行"。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        empty = _register(client, _name("empty"), "")
        assert empty.status_code == 403
        assert empty.json()["detail"] == invite_service.REGISTER_ERRORS["invalid"][1]
        assert _register(client, _name("ghost"), "NOT-A-REAL-CODE").status_code == 403


def test_local_dev_fallback_identity_survives_when_debug(monkeypatch):
    """验收 6 的另一半：DEBUG=true 的 local-dev 兜底身份保持不变（本地开发依赖它）。"""
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        created = client.post("/api/projects/", json={"name": _name("local-dev-project")})
        assert created.status_code == 200


# ── 登录（验收 7–11） ─────────────────────────────────────


def test_registered_account_can_login_with_username_and_password(monkeypatch):
    """验收 7：用户名+密码登录保持可用。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    username = _name("pwlogin")
    with TestClient(app) as client:
        assert _register(client, username, _mint()[0]).status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").status_code == 401
        assert _login(client, username).status_code == 200
        assert client.get("/api/auth/me").json()["data"]["username"] == username


def test_bound_invite_can_be_used_as_login_credential(monkeypatch):
    """验收 8：bound 码只提交它 → 登录**绑定账号**（C 语义）。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    username = _name("bound")
    code = _mint()[0]
    with TestClient(app) as client:
        assert _register(client, username, code).status_code == 200
        client.post("/api/auth/logout")
        response = _login_with_invite(client, code)
        assert response.status_code == 200
        assert response.json()["data"]["username"] == username
        assert client.get("/api/auth/me").json()["data"]["username"] == username


def test_pending_invite_cannot_login_and_tells_user_to_register(monkeypatch):
    """验收 9：pending 码不能登录，文案要引导去注册。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    with TestClient(app) as client:
        response = _login_with_invite(client, code)
        assert response.status_code == 403
        assert response.json()["detail"] == invite_service.PENDING_LOGIN_MESSAGE
        assert client.get("/api/auth/me").status_code == 401


def test_disabled_invite_cannot_login_but_password_still_works(monkeypatch):
    """验收 10：停用码不能登录，但**密码仍能登录**（停用码 ≠ 封号）。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    username = _name("disable")
    code = _mint()[0]
    with TestClient(app) as client:
        assert _register(client, username, code).status_code == 200
        client.post("/api/auth/logout")
        row = invites_dao.find_by_code(code)
        invites_dao.disable_invite(invite_id=row["id"])
        assert invites_dao.find_by_code(code)["status"] == "disabled"

        rejected = _login_with_invite(client, code)
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == invite_service.DISABLED_LOGIN_MESSAGE

        assert _login(client, username).status_code == 200
        assert client.get("/api/auth/me").json()["data"]["username"] == username


def test_legacy_env_invite_channel_is_unchanged(monkeypatch):
    """验收 11（回归钉子）：老 `.env` 码的免密码通道行为不变；但它不再能注册。"""
    monkeypatch.setattr(settings, "debug", False)
    username = _name("legacy")
    user_id = users_dao.create_user(username, "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"legacy-code-x:{user_id}")
    with TestClient(app) as client:
        headers = {"X-NovelCraft-Invite": "legacy-code-x"}
        created = client.post(
            "/api/projects/", json={"name": _name("legacy-project")}, headers=headers
        )
        assert created.status_code == 200
        project_id = created.json()["data"]["id"]
        # 同一项目的 owner 校验仍按映射出的账号生效；无凭据一律 401
        assert client.get(f"/api/projects/{project_id}", headers=headers).status_code == 200
        assert client.get(f"/api/projects/{project_id}").status_code == 401

        # 登录接口只提交老码：进映射账号（PRD 3.4）
        authenticated = _login_with_invite(client, "legacy-code-x")
        assert authenticated.status_code == 200
        assert authenticated.json()["data"]["id"] == user_id
        client.post("/api/auth/logout")

        # 但老码不能用于注册：注册只认库里的 pending 一次性码（PRD 3.1）
        assert _register(client, _name("legacy-reg"), "legacy-code-x").status_code == 403


# ── 可见性与操控（验收 12–13，含 3.3 用户名规则） ──────────


def test_invite_list_never_exposes_plaintext_and_new_codes_are_unique():
    """验收 12：list 能看到 hint/label/状态/绑定账号/时间，但看不到明文。"""
    first_batch = _mint(3, "list-batch-1")
    rows = invites_dao.list_invites()
    assert rows
    serialized = repr(rows)
    for code in first_batch:
        assert code not in serialized, "list 绝不能出现明文"
        assert invites_dao.hash_code(code) not in serialized, "list 也不应出现哈希"
        stored = invites_dao.find_by_code(code)
        assert stored["label"] == "list-batch-1"
        assert stored["status"] == "pending"
    hints = {row["code_hint"] for row in rows}
    for code in first_batch:
        assert code[-4:] in hints
    # 再次生成：明文与已发出的任何码都不同
    second_batch = _mint(3, "list-batch-2")
    assert set(first_batch).isdisjoint(set(second_batch))
    assert len(set(first_batch) | set(second_batch)) == 6


def test_my_invites_returns_hint_and_bound_at_without_plaintext(monkeypatch):
    """验收 13：`/invites/mine` 只给 hint 与绑定时间（不回显明文 / 哈希）。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    username = _name("mine")
    code = _mint()[0]
    with TestClient(app) as client:
        assert _register(client, username, code).status_code == 200
        response = client.get("/api/auth/invites/mine")
        assert response.status_code == 200
        data = response.json()["data"]
        assert isinstance(data, list) and len(data) == 1
        assert data[0]["code_hint"] == code[-4:]
        assert data[0]["bound_at"]
        assert code not in response.text
        assert invites_dao.hash_code(code) not in response.text

        # 没绑定过码的账号：只能从**非注册路径**产生（注册必然消耗一个码）。
        # 真实场景就是老 .env 通道进来的用户，或历史上手工建的账号。
        client.post("/api/auth/logout")
        legacy_name = _name("mine-legacy")
        users_dao.create_user(legacy_name, "strong-pass-1")
        assert _login(client, legacy_name).status_code == 200
        assert client.get("/api/auth/invites/mine").json()["data"] == []


def test_my_invites_requires_login(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        assert client.get("/api/auth/invites/mine").status_code == 401


def test_username_available_endpoint_reports_availability_and_reason(monkeypatch):
    """PRD 3.3：占用 / 不合规都算不可用，并给出原因。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    taken = _name("takenname")
    with TestClient(app) as client:
        assert _register(client, taken, _mint()[0]).status_code == 200

        free = client.get("/api/auth/username-available", params={"username": _name("free")})
        assert free.status_code == 200
        assert free.json()["data"] == {"available": True, "reason": ""}

        busy = client.get("/api/auth/username-available", params={"username": taken.upper()})
        assert busy.json()["data"]["available"] is False
        assert busy.json()["data"]["reason"] == invite_service.USERNAME_TAKEN_MESSAGE

        illegal = client.get("/api/auth/username-available", params={"username": "bad name"})
        assert illegal.json()["data"]["available"] is False
        assert illegal.json()["data"]["reason"]


def test_username_uniqueness_is_case_insensitive(monkeypatch):
    """PRD 3.3：`W` 与 `w` 视为同一个用户名。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    registered = _name("Case").upper()
    with TestClient(app) as client:
        assert _register(client, registered, _mint()[0]).status_code == 200
        conflict = _register(client, registered.lower(), _mint()[0])
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == invite_service.USERNAME_TAKEN_MESSAGE
        # 用小写也能登录到同一个账号（查重与登录同一口径）
        assert _login(client, registered.lower()).json()["data"]["username"] == registered


def test_username_rule_accepts_allowed_characters_and_rejects_others():
    """PRD 3.3 规则本身（中文/字母/数字/_/-；禁止空格、全角空格与其它符号）。"""
    for good in ["中文用户名", "abc_123", "A-B-c", "写手-01"]:
        ok, reason = invite_service.validate_username(good)
        assert ok, f"{good} 应合法：{reason}"
    for bad in ["ab", "a" * 31, "abc\u3000def", "bad name", "bad!name", "bad@name", "", " a"]:
        ok, reason = invite_service.validate_username(bad)
        assert not ok and reason, f"{bad!r} 应被拒绝"


# ── DAO / 服务层语义 ──────────────────────────────────────


def test_bind_invite_is_atomic_and_only_from_pending():
    """一码一人：绑定是条件更新，第二次必然失败（并发注册的底层保证）。"""
    code = _mint()[0]
    row = invites_dao.find_by_code(code)
    assert row["status"] == "pending"
    assert invites_dao.bind_invite(row["code_hash"], "someone-1") is True
    assert invites_dao.bind_invite(row["code_hash"], "someone-2") is False
    stored = invites_dao.find_by_code(code)
    assert stored["status"] == "bound" and stored["bound_user_id"] == "someone-1"
    assert invites_dao.bind_invite("not-a-real-hash", "someone-3") is False
    assert invites_dao.bind_invite(row["code_hash"], "") is False

    disabled_code = _mint()[0]
    disabled_row = invites_dao.find_by_code(disabled_code)
    invites_dao.disable_invite(invite_id=disabled_row["id"])
    assert invites_dao.bind_invite(disabled_row["code_hash"], "someone-4") is False


def test_invite_supports_expiry_and_limited_registration_uses(monkeypatch):
    """A generated invite can be used exactly max_uses times and then closes."""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    code = invites_dao.generate_invites(
        1, "limited", "pytest", expires_at=expires_at, max_uses=2
    )[0]
    with TestClient(app) as client:
        assert _register(client, _name("limited-a"), code).status_code == 200
        assert _register(client, _name("limited-b"), code).status_code == 200
        rejected = _register(client, _name("limited-c"), code)
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == invite_service.REGISTER_ERRORS["used"][1]
    stored = invites_dao.find_by_code(code)
    assert stored["used_count"] == 2
    assert stored["max_uses"] == 2
    assert stored["status"] == invites_dao.STATUS_BOUND


@pytest.mark.parametrize("registrations", [1, 2])
def test_shared_invite_never_authenticates_as_first_registered_account(monkeypatch, registrations):
    """Shared registration codes are never a key to any registrant's account."""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = invites_dao.generate_invites(1, "shared-registration-only", "pytest", max_uses=2)[0]
    with TestClient(app) as owner:
        registered = _register(owner, _name("shared-first"), code)
        assert registered.status_code == 200
        project = owner.post("/api/projects/", json={"name": _name("shared-private")})
        assert project.status_code == 200
        project_id = project.json()["data"]["id"]
    if registrations == 2:
        with TestClient(app) as second:
            assert _register(second, _name("shared-second"), code).status_code == 200

    stored = invites_dao.find_by_code(code)
    assert stored["status"] == (invites_dao.STATUS_PENDING if registrations == 1 else invites_dao.STATUS_BOUND)
    with TestClient(app) as stranger:
        login = _login_with_invite(stranger, code)
        assert login.status_code == 403
        assert stranger.get("/api/auth/me").status_code == 401
        denied = stranger.get(
            f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": code}
        )
        assert denied.status_code == 401
        assert stranger.get("/api/auth/permissions", headers={"X-NovelCraft-Invite": code}).status_code == 401


def test_expired_invite_is_rejected_without_consuming_it(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    row = invites_dao.find_by_code(code)
    with get_conn() as conn:
        conn.execute(
            "UPDATE invites SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", row["id"]),
        )
    with TestClient(app) as client:
        rejected = _register(client, _name("expired"), code)
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == invite_service.REGISTER_ERRORS["expired"][1]
    assert invites_dao.find_by_code(code)["used_count"] == 0


def test_expired_bound_invite_cannot_be_used_as_login_credential(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    username = _name("expired-login")
    with TestClient(app) as client:
        assert _register(client, username, code).status_code == 200
        client.post("/api/auth/logout")
        row = invites_dao.find_by_code(code)
        with get_conn() as conn:
            conn.execute(
                "UPDATE invites SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", row["id"]),
            )
        response = _login_with_invite(client, code)
    assert response.status_code == 403
    assert response.json()["detail"] == invite_service.EXPIRED_LOGIN_MESSAGE


def test_limited_invite_atomic_consumption_never_exceeds_max_uses():
    code = invites_dao.generate_invites(1, "atomic-limited", "pytest", max_uses=2)[0]
    row = invites_dao.find_by_code(code)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(
            lambda index: invites_dao.bind_invite(row["code_hash"], f"atomic-user-{index}"),
            range(5),
        ))
    assert sum(results) == 2
    stored = invites_dao.find_by_code(code)
    assert stored["used_count"] == 2
    assert stored["status"] == invites_dao.STATUS_BOUND


def test_invite_lifecycle_options_reject_invalid_values():
    with pytest.raises(ValueError, match="max_uses"):
        invites_dao.generate_invites(1, max_uses=0)
    with pytest.raises(ValueError, match="ISO-8601"):
        invites_dao.generate_invites(1, expires_at="tomorrow")
    with pytest.raises(ValueError, match="晚于当前时间"):
        invites_dao.generate_invites(1, expires_at="2000-01-01T00:00:00Z")


def test_init_db_migrates_legacy_invites_without_losing_rows_and_is_idempotent(tmp_path, monkeypatch):
    """An old invites table must receive columns before its new index is created."""
    legacy_path = tmp_path / "legacy-invites.db"
    columns = (
        "id", "code_hash", "code_hint", "label", "status", "bound_user_id",
        "bound_at", "created_at", "created_by",
    )
    legacy_rows = [
        ("legacy-pending", "test-pending-hash", "PEND", "pending fixture", "pending", "", "",
         "2026-01-01T00:00:00Z", "pytest"),
        ("legacy-bound", "test-bound-hash", "BOND", "bound fixture", "bound", "test-owner",
         "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", "pytest"),
    ]
    conn = sqlite3.connect(legacy_path)
    try:
        conn.execute(
            "CREATE TABLE invites (id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE, "
            "code_hint TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'pending', bound_user_id TEXT NOT NULL DEFAULT '', "
            "bound_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
            "created_by TEXT NOT NULL DEFAULT '')"
        )
        conn.executemany("INSERT INTO invites VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", legacy_rows)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(app_db, "DB_PATH", legacy_path)

    snapshots = []
    for _ in range(2):
        app_db.init_db()
        conn = sqlite3.connect(legacy_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in conn.execute("SELECT * FROM invites ORDER BY id")]
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(invites)")}
        finally:
            conn.close()
        assert "idx_invites_expiry" in indexes
        assert [{key: row[key] for key in columns} for row in rows] == sorted(
            [dict(zip(columns, row)) for row in legacy_rows], key=lambda row: row["id"]
        )
        assert all(row["expires_at"] == "" and row["max_uses"] == 1 and row["used_count"] == 0 for row in rows)
        snapshots.append(rows)
    assert snapshots[0] == snapshots[1]


def test_resolve_credential_classifies_all_five_states(monkeypatch):
    monkeypatch.setattr(settings, "invite_tokens", "env-code-1:env-user")
    pending = _mint()[0]
    bound = _mint()[0]
    bound_row = invites_dao.find_by_code(bound)
    invites_dao.bind_invite(bound_row["code_hash"], "bound-user")
    disabled = _mint()[0]
    disabled_row = invites_dao.find_by_code(disabled)
    invites_dao.disable_invite(invite_id=disabled_row["id"])

    assert invite_service.resolve_credential("") == ("invalid", None)
    assert invite_service.resolve_credential("no-such-code") == ("invalid", None)
    assert invite_service.resolve_credential("env-code-1") == ("legacy", "env-user")
    assert invite_service.resolve_credential(pending) == ("pending", None)
    assert invite_service.resolve_credential(bound) == ("bound", "bound-user")
    assert invite_service.resolve_credential(disabled) == ("disabled", None)
    # 手抄码：大小写不敏感 + 去首尾空白
    assert invite_service.resolve_credential(f"  {pending.lower()}  ") == ("pending", None)


def test_invite_lookup_failure_does_not_break_legacy_channel(monkeypatch):
    """PRD 四·异常与降级：邀请码表坏了，DB 码报 403，老 `.env` 通道必须照常工作。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "env-code-2:env-user-2")

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such table: invites")

    monkeypatch.setattr(invites_dao, "find_by_code", _boom)
    assert invite_service.resolve_credential("ANYCODE1234") == ("invalid", None)
    assert invite_service.resolve_credential("env-code-2") == ("legacy", "env-user-2")
    assert invite_service.check_invite_for_registration("ANYCODE1234")[0] == "invalid"
    assert invite_service.bound_invites_for_user("whoever") == []

    with TestClient(app) as client:
        headers = {"X-NovelCraft-Invite": "env-code-2"}
        created = client.post(
            "/api/projects/", json={"name": _name("degraded")}, headers=headers
        )
        assert created.status_code == 200, "邀请码表异常不得把老通道一起弄挂"
        assert _register(client, _name("degraded-reg"), "ANYCODE1234").status_code == 403
        assert _login_with_invite(client, "ANYCODE1234").status_code in (401, 403)


def test_registration_and_invite_flow_never_writes_style_tables(monkeypatch):
    """PRD 四·不变量：注册/邀请码**绝不能**写 style_profiles / style_bindings。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")

    def _style_counts() -> tuple[int, int]:
        with get_conn() as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM style_profiles").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM style_bindings").fetchone()[0],
            )

    before = _style_counts()
    code = _mint()[0]
    with TestClient(app) as client:
        assert _register(client, _name("nostyle"), code).status_code == 200
        assert client.get("/api/auth/invites/mine").status_code == 200
        assert _login_with_invite(client, code).status_code == 200
        assert (
            client.get("/api/auth/username-available", params={"username": _name("probe")}).status_code
            == 200
        )
    assert _style_counts() == before


# ── 注销与孤儿（验收 17） ─────────────────────────────────


def test_account_deletion_leaves_no_orphan_owner(monkeypatch):
    """验收 17：没有作品的账号可以注销，且不会留下孤儿 owner。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        assert _register(client, _name("del"), _mint()[0]).status_code == 200
        user_id = client.get("/api/auth/me").json()["data"]["id"]
        assert client.delete("/api/auth/account").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
    assert users_dao.get_user(user_id) is None
    with get_conn() as conn:
        owned = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE owner_id = ?", (user_id,)
        ).fetchone()[0]
        orphans = [
            row
            for row in check_orphan_owners.find_orphan_owners(conn, [])
            if row["owner_id"] == user_id
        ]
    assert owned == 0 and orphans == []
    # 根治而非"小心别调用"：造成孤儿的老死代码必须已经不存在
    assert not hasattr(users_dao, "delete_user")


# ── 归属迁移（验收 14–16，全部在临时夹具库上跑） ──────────

LEGACY_OWNER = "beta-user-001"
LEGACY_ORPHAN = "c7836e0fa58a"
TARGET_OWNER = "ec1ec540af85"


def _build_legacy_fixture(path: Path) -> None:
    """最小夹具库：形状与真实库一致，数字取真实值（6+1 项目 / 19 候选 / 4 会话）。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE outline_candidates (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
        CREATE TABLE user_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL);
        """
    )
    conn.execute("INSERT INTO users (id, username) VALUES (?, ?)", (TARGET_OWNER, "W"))
    for index in range(6):
        conn.execute(
            "INSERT INTO projects (id, owner_id, name) VALUES (?, ?, ?)",
            (f"beta-project-{index}", LEGACY_OWNER, f"beta {index}"),
        )
    conn.execute(
        "INSERT INTO projects (id, owner_id, name) VALUES (?, ?, ?)",
        ("orphan-private", LEGACY_ORPHAN, "private"),
    )
    conn.execute(
        "INSERT INTO projects (id, owner_id, name) VALUES (?, ?, ?)",
        ("own-project", TARGET_OWNER, "own"),
    )
    for index in range(19):
        conn.execute(
            "INSERT INTO outline_candidates (id, user_id) VALUES (?, ?)",
            (f"candidate-{index}", LEGACY_OWNER),
        )
    for index in range(4):
        conn.execute(
            "INSERT INTO user_sessions (token_hash, user_id) VALUES (?, ?)",
            (f"session-token-{index}", TARGET_OWNER),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "beta-user-001.db"
    _build_legacy_fixture(path)
    return path


def _owner_snapshot(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        projects = dict(
            conn.execute("SELECT owner_id, COUNT(*) FROM projects GROUP BY owner_id").fetchall()
        )
        candidates = dict(
            conn.execute("SELECT user_id, COUNT(*) FROM outline_candidates GROUP BY user_id").fetchall()
        )
        sessions = conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0]
    finally:
        conn.close()
    return {"projects": projects, "candidates": candidates, "sessions": int(sessions)}


def test_migration_dry_run_writes_nothing(legacy_db: Path, capsys):
    """验收 14：dry-run 不写库，只打印将改动的行数。"""
    before = _owner_snapshot(legacy_db)
    assert migrate_legacy_owner.main(["--db", str(legacy_db)]) == 0
    output = capsys.readouterr().out
    assert "预演" in output
    assert "projects.owner_id" in output and "7" in output
    assert "outline_candidates.user_id" in output and "19" in output
    assert _owner_snapshot(legacy_db) == before, "dry-run 不得写库"
    assert not (legacy_db.parent / "backups").exists(), "dry-run 不该产生备份"


def test_migration_apply_backs_up_and_keeps_user_sessions(legacy_db: Path, capsys):
    """验收 14 + 15：--apply 前有备份；迁移后 8 个项目都归管理员；会话行数不变。"""
    before = _owner_snapshot(legacy_db)
    assert migrate_legacy_owner.main(["--db", str(legacy_db), "--apply"]) == 0
    output = capsys.readouterr().out
    assert "[备份]" in output

    backups = sorted((legacy_db.parent / "backups").glob(f"{legacy_db.name}.*.bak"))
    assert len(backups) == 1 and backups[0].stat().st_size > 0, "备份文件必须真的被创建"

    after = _owner_snapshot(legacy_db)
    # 6 个 beta-user-001 + 1 个孤儿 + 1 个原有 = 8，全部收敛到同一个身份
    assert after["projects"] == {TARGET_OWNER: 8}
    assert after["candidates"] == {TARGET_OWNER: 19}
    # user_sessions 绝对不动（否则作者会被踢下线）
    assert after["sessions"] == before["sessions"] == 4


def test_migration_apply_is_idempotent(legacy_db: Path, capsys):
    """验收 14：再跑一次 --apply 改动 0 行。"""
    assert migrate_legacy_owner.main(["--db", str(legacy_db), "--apply"]) == 0
    capsys.readouterr()
    assert migrate_legacy_owner.main(["--db", str(legacy_db), "--apply"]) == 0
    output = capsys.readouterr().out
    assert "已经是最新状态" in output
    conn = sqlite3.connect(legacy_db)
    try:
        assert migrate_legacy_owner.plan_changes(conn) == {
            "projects.owner_id": 0,
            "outline_candidates.user_id": 0,
        }
    finally:
        conn.close()


def test_orphan_report_is_clean_after_migration(legacy_db: Path, capsys):
    """验收 16：迁移前报告孤儿，迁移后输出"无孤儿"。"""
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    try:
        with_admin = check_orphan_owners.find_orphan_owners(conn, [LEGACY_OWNER])
        assert [row["owner_id"] for row in with_admin] == [LEGACY_ORPHAN]
        without_admin = check_orphan_owners.find_orphan_owners(conn, [])
        # 白名单为空时：6 个 beta-user-001 项目 + 1 个孤儿项目 = 7。
        # `ec1ec540af85` 在 users 表里有行，所以它不是孤儿。
        assert len(without_admin) == 7
    finally:
        conn.close()

    assert migrate_legacy_owner.main(["--db", str(legacy_db), "--apply"]) == 0
    capsys.readouterr()
    assert check_orphan_owners.main(["--db", str(legacy_db), "--admin-ids", LEGACY_OWNER]) == 0
    assert "无孤儿" in capsys.readouterr().out
    assert check_orphan_owners.main(["--db", str(legacy_db), "--admin-ids", ""]) == 0


def test_orphan_report_flags_orphans_with_exit_code_one(legacy_db: Path, capsys):
    """孤儿存在时报告内容可读，且退出码为 1（可当交付前检查项的闸门）。"""
    assert check_orphan_owners.main(["--db", str(legacy_db), "--admin-ids", LEGACY_OWNER]) == 1
    output = capsys.readouterr().out
    assert LEGACY_ORPHAN in output and "orphan-private" in output
    assert "本脚本不会修改任何数据" in output


def test_admin_ids_parser_ignores_blanks_and_duplicates():
    assert check_orphan_owners.parse_admin_ids(" local-dev , beta-user-001 ,,local-dev ") == [
        "local-dev",
        "beta-user-001",
    ]
    assert check_orphan_owners.parse_admin_ids("") == []


# ═══════════════════════════════════════════════════════════
# 第二轮（T4 设计审查 / T5 对抗测试审查）发现问题的钉子测试
# 每个测试对应一个**真实存在过**的缺陷，防止它回来。
# ═══════════════════════════════════════════════════════════


def _register_new(client: TestClient) -> tuple[str, str, str]:
    """注册一个全新账号，返回 (用户名, 账号 id, 用掉的码)。"""
    code = _mint()[0]
    name = _name("pin")
    response = _register(client, name, code)
    assert response.status_code == 200, response.text
    return name, str(response.json()["data"]["id"]), code


def test_deleting_account_disables_its_bound_invite(monkeypatch):
    """T5-BUG-1：注销后**不能**留下指向已消失账号的死码。

    悬空绑定既登不进、也重新注册不了，是一把永远打不开门的钥匙，
    而它不会出现在 projects 的孤儿扫描里。
    """
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        _, uid, code = _register_new(client)
        assert invites_dao.find_by_code(code)["status"] == invites_dao.STATUS_BOUND
        assert client.delete("/api/auth/account").status_code == 200

        row = invites_dao.find_by_code(code)
        assert row is not None, "码本身不该被删掉（要留痕）"
        assert row["status"] == invites_dao.STATUS_DISABLED
        assert row["bound_user_id"] == "", "必须清空悬空绑定"
        assert _login_with_invite(client, code).status_code == 403
        assert _register(client, _name("pin2"), code).status_code == 403

    with get_conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM invites WHERE bound_user_id = ?", (uid,)
        ).fetchone()[0]
    assert left == 0


def test_dangling_invite_binding_is_reported_by_health_check():
    """T5-BUG-1：体检脚本必须能看见"悬空邀请码绑定"这一类孤儿。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO invites(id, code_hash, code_hint, status, bound_user_id, created_at)"
            " VALUES ('pin-dangling', 'pin-dangling-hash', 'Z9Z9', 'disabled', 'ghost-user', 'now')"
        )
    with get_conn() as conn:
        rows = check_orphan_owners.find_dangling_invites(conn)
        reports = check_orphan_owners.find_all_orphans(conn, ["local-dev"])
    assert any(row["owner_id"] == "ghost-user" for row in rows)
    assert "invites(dangling bound)" in reports


def test_bind_failure_rolls_back_account_and_keeps_code(monkeypatch):
    """T5-BUG-2：绑码时抛 DB 异常，不能留下"用户名已被占用但注册说失败"的僵尸账号。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    code = _mint()[0]
    name = _name("zombie")

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(invite_service, "bind_invite_to_user", _boom)
    with TestClient(app) as client:
        response = _register(client, name, code)

    assert response.status_code == 503, response.text
    assert users_dao.find_user(name) is None, "失败路径不能留下账号"
    assert invites_dao.find_by_code(code)["status"] == invites_dao.STATUS_PENDING, "失败不该消耗码"


def test_init_db_refuses_case_duplicate_usernames_with_readable_error():
    """T5-BUG-3：老库里有仅大小写不同的用户名时，要抛**可读**错误而不是裸 IntegrityError。

    否则后端起不来的原因完全不可诊断。
    """
    with get_conn() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_users_username_lower")
        conn.execute(
            "INSERT INTO users(id, username, password_hash, is_admin, created_at)"
            " VALUES ('pin-dup-1', 'CaseDup', 'x', 0, 'now')"
        )
        conn.execute(
            "INSERT INTO users(id, username, password_hash, is_admin, created_at)"
            " VALUES ('pin-dup-2', 'casedup', 'x', 0, 'now')"
        )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            app_db.init_db()
        assert "仅大小写不同" in str(excinfo.value)
        assert "CaseDup" in str(excinfo.value)
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE id IN ('pin-dup-1', 'pin-dup-2')")
        app_db.init_db()  # 清理后必须能正常建成索引（幂等）
    with get_conn() as conn:
        names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='users'"
            )
        ]
    assert "idx_users_username_lower" in names


def test_username_available_does_not_lie_about_padded_names(monkeypatch):
    """T5-BUG-4：占用查询必须与注册同一个 strip 口径，否则会谎报"可用"。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        name, _, _ = _register_new(client)
        data = client.get(
            "/api/auth/username-available", params={"username": f"  {name}  "}
        ).json()["data"]
    assert data["available"] is False
    assert data["reason"]


def test_login_strips_username_like_registration(monkeypatch):
    """T5-BUG-5：注册会 strip、登录不 strip，会让用户"按自己输入的样子"登不进去。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        name, _, _ = _register_new(client)
        assert _login(client, f"  {name}  ").status_code == 200


def test_register_bucket_is_independent_from_login(monkeypatch):
    """T5-BUG-6：别人的失败登录不能把新用户注册锁死（共享出口 IP 场景）。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    monkeypatch.setattr(settings, "rate_limit", 3)
    import main as main_module

    main_module._auth_windows.clear()
    main_module._register_windows.clear()
    with TestClient(app) as client:
        for _ in range(4):
            assert client.post(
                "/api/auth/login", json={"username": "nobody", "password": "wrong-pass-1"}
            ).status_code in (401, 429)
        # 登录桶已满，注册必须仍然可用
        code = _mint()[0]
        assert _register(client, _name("rl"), code).status_code == 200

    # 注册自己仍然会被限流（不是把它变成无限）
    main_module._register_windows.clear()
    with TestClient(app) as client:
        statuses = [_register(client, _name("rl2"), _mint()[0]).status_code for _ in range(4)]
    assert 429 in statuses


def test_legacy_env_code_is_rejected_for_registration(monkeypatch):
    """T5-BUG-7：同一串码不能既走老通道（登录进别人）又能注册（绑给新账号）。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "shadow-code:legacy-target")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users(id, username, password_hash, is_admin, created_at)"
            " VALUES ('legacy-target', 'legacy-target-user', ?, 0, 'now')",
            (users_dao.hash_password("strong-pass-1"),),
        )
        conn.execute(
            "INSERT INTO invites(id, code_hash, code_hint, label, status, created_at)"
            " VALUES ('pin-shadow', ?, 'code', 'shadow', 'pending', 'now')",
            (invites_dao.hash_code("shadow-code"),),
        )
    with TestClient(app) as client:
        # 注册必须被拒（老通道优先，不允许双真值来源）
        assert _register(client, _name("shadow"), "shadow-code").status_code == 403
        # 而它作为老通道仍然能登录进映射的账号（Q2=A 行为不变）
        response = _login_with_invite(client, "shadow-code")
        assert response.status_code == 200
        assert response.json()["data"]["id"] == "legacy-target"


def test_delete_account_refused_when_only_user_level_assets_exist(monkeypatch):
    """T5-BUG-8：注销只查 projects 会漏掉素材库/大纲候选，注销后它们会变成孤儿。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        _, uid, _ = _register_new(client)
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO reference_works(id, project_id, owner_id, name, raw_text, created_at, updated_at)"
                " VALUES ('pin-rw-1', 'ghost-project', ?, 'x', 'y', 'now', 'now')",
                (uid,),
            )
            conn.execute(
                "INSERT INTO outline_candidates"
                " (id, project_id, user_id, base_outline_hash, candidate_outline_json, created_at, updated_at)"
                " VALUES ('pin-oc-1', 'ghost-project', ?, 'h', '{}', 'now', 'now')",
                (uid,),
            )
        response = client.delete("/api/auth/account")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["referenceWorks"] == 1
    assert detail["outlineCandidates"] == 1
    assert "参考档案" in detail["message"] and "大纲候选" in detail["message"]


def test_oversized_inputs_return_400_without_echoing_password(monkeypatch):
    """T5-BUG-9：超长输入不能走框架 422（detail 是数组、还会回显密码明文）。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    long_password = "p" * 600
    payloads = [
        {"username": "u" * 300, "password": "strong-pass-1", "invite": "x"},
        {"username": "okname", "password": long_password, "invite": "x"},
        {"username": "okname", "password": "strong-pass-1", "invite": "i" * 300},
    ]
    with TestClient(app) as client:
        for payload in payloads:
            response = client.post("/api/auth/register", json=payload)
            assert response.status_code == 400, response.text
            assert isinstance(response.json()["detail"], str)
            assert long_password not in response.text


def test_invite_login_distinguishes_invalid_from_missing_legacy_account(monkeypatch):
    """T4-P0：前端必须能区分这两件事，否则"码写错"会被当成"老码缺账号"而静默放行。"""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "legacy-no-account:vanish-user")
    with TestClient(app) as client:
        invalid = _login_with_invite(client, "totally-wrong-code")
        missing = _login_with_invite(client, "legacy-no-account")
    assert invalid.status_code == 403
    assert invalid.json()["detail"]["code"] == "invite_invalid"
    assert missing.status_code == 401
    assert missing.json()["detail"]["code"] == "legacy_account_missing"

