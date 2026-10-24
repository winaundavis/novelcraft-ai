"""管理入口的**服务端判定**（`/auth/permissions` 与模型配置接口的 403）。

背景：前端要把「模型设置」只给管理员看。前端显示什么必须与服务端放行什么
**同源**，所以这里钉住三件事：

1. `/auth/permissions` 的判定与 `require_maintainer` 完全一致（都走
   `current_user_id` + `is_admin_user`），不会出现「菜单藏了但接口放行」或
   「接口 403 但菜单还在」；
2. 它覆盖**没有会话**的通道（老 `.env` 邀请码走请求头）—— `/auth/me` 看不到这种凭据，
   所以只看 `me` 会让用老码登录的管理员看不到管理入口；
3. `/auth/me` 与登录/注册响应里的 `isAdmin` 也走同一个 `is_admin_user`，
   不会出现「同一个账号两处说法不同」。

测试库由 `tests/conftest.py` 全进程共用一个临时文件，所以用户名一律带递增后缀。
"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.terms import TERMS_VERSION
from app.dao import invites as invites_dao
from app.dao import users as users_dao
from main import app

_COUNTER = itertools.count(1)


def _name(prefix: str) -> str:
    return f"{prefix}-{next(_COUNTER)}"


@pytest.fixture(autouse=True)
def _relax_auth_rate_limit(monkeypatch):
    """本文件认证请求较多，放宽限流，避免 429 干扰用例语义。"""
    monkeypatch.setattr(settings, "rate_limit", 100000)


def _isolate_admin_sources(monkeypatch, *, allowed: str = "") -> None:
    """关掉 DEBUG 的 `local-dev` 兜底并把管理员名单换成显式值。

    两者必须同时处理：`current_user_id` 在 `debug=True` 且无凭据时返回 `local-dev`，
    而 `is_admin_user` 又认 `ADMIN_USER_IDS`，漏掉任一个都会让用例失去意义。
    """
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "admin_user_ids", allowed)


def _register_ordinary_user(client: TestClient) -> dict:
    """注册一个**普通**账号（`is_admin=0`、不在管理员名单里），返回账号信息。"""
    code = invites_dao.generate_invites(1, "pytest-admin-gate", "pytest")[0]
    response = client.post(
        "/api/auth/register",
        json={"username": _name("gate"), "password": "strong-pass-1", "invite": code, "termsAccepted": True, "termsVersion": TERMS_VERSION},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ── 1. 判定与凭据解析 ──────────────────────────────────────────────


def test_permissions_requires_a_credential_when_debug_is_off(monkeypatch):
    """没有任何凭据 → 401（前端据此保守地当作非管理员）。"""
    _isolate_admin_sources(monkeypatch)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        response = client.get("/api/auth/permissions")
    assert response.status_code == 401, response.text


def test_permissions_is_false_for_an_ordinary_account(monkeypatch):
    """普通账号 → `isAdmin: false`（这就是普通用户看不到管理入口的依据）。"""
    _isolate_admin_sources(monkeypatch)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        account = _register_ordinary_user(client)
        response = client.get("/api/auth/permissions")
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["userId"] == account["id"]
    assert payload["isAdmin"] is False


def test_permissions_is_true_for_an_account_flagged_admin_in_the_database(monkeypatch):
    """`users.is_admin = 1` 的账号 → true（数据库通道）。"""
    _isolate_admin_sources(monkeypatch)
    monkeypatch.setattr(settings, "invite_tokens", "")
    uid = users_dao.create_user(_name("dbadmin"), "strong-pass-1", is_admin=True)
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login", json={"username": users_dao.get_user(uid)["username"], "password": "strong-pass-1", "termsAccepted": True, "termsVersion": TERMS_VERSION}
        ).status_code == 200
        response = client.get("/api/auth/permissions")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"userId": uid, "isAdmin": True}


def test_permissions_works_for_legacy_invite_header_without_a_session(monkeypatch):
    """**没有会话**、只有老 `.env` 邀请码请求头时也必须能判定管理员。

    这是 `/auth/me` 覆盖不到的通道（它只认会话 cookie）。若只看 `me`，
    用老码登录的管理员会被前端当成普通用户，管理入口凭空消失。
    """
    uid = users_dao.create_user(_name("legacyadmin"), "strong-pass-1", is_admin=True)
    _isolate_admin_sources(monkeypatch)
    monkeypatch.setattr(settings, "invite_tokens", f"OLDCODE:{uid}")
    with TestClient(app) as client:
        # 先确认 `/auth/me` 在这个通道下确实看不到（这是本用例存在的前提）
        assert client.get("/api/auth/me").status_code == 401
        response = client.get("/api/auth/permissions", headers={"X-NovelCraft-Invite": "OLDCODE"})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"userId": uid, "isAdmin": True}


def test_permissions_counts_debug_local_dev_as_admin(monkeypatch):
    """DEBUG 单人模式（`local-dev` 兜底）仍然是管理员 —— 明确钉住，避免被"顺手修掉"。"""
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "admin_user_ids", "local-dev")
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        response = client.get("/api/auth/permissions")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["isAdmin"] is True


# ── 2. 单一真值来源：me / login / permissions 说法必须一致 ──────────


def test_me_and_login_report_the_same_admin_flag_as_permissions(monkeypatch):
    """同一个账号在 `/auth/me`、登录响应、`/auth/permissions` 三处的 `isAdmin` 必须相同。

    历史隐患：`me` 与登录响应只读 `users.is_admin`，而 `require_maintainer` 还认
    `ADMIN_USER_IDS`。于是把账号写进 `.env` 名单后，接口放行、菜单却依旧不显示。
    """
    monkeypatch.setattr(settings, "invite_tokens", "")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "admin_user_ids", "")
    code = invites_dao.generate_invites(1, "pytest-admin-gate", "pytest")[0]
    username = _name("envadmin")
    with TestClient(app) as client:
        registered = client.post(
            "/api/auth/register",
            json={"username": username, "password": "strong-pass-1", "invite": code, "termsAccepted": True, "termsVersion": TERMS_VERSION},
        )
        assert registered.status_code == 200, registered.text
        uid = registered.json()["data"]["id"]
        # 注册时还不是管理员
        assert registered.json()["data"]["isAdmin"] is False

        # 把这一个账号加进 `.env` 名单（接口放行的那条通道），三处必须同时翻转。
        monkeypatch.setattr(settings, "admin_user_ids", f"other-user,{uid}")
        assert client.get("/api/auth/me").json()["data"]["isAdmin"] is True
        assert client.post(
            "/api/auth/login", json={"username": username, "password": "strong-pass-1", "termsAccepted": True, "termsVersion": TERMS_VERSION}
        ).json()["data"]["isAdmin"] is True
        assert client.get("/api/auth/permissions").json()["data"]["isAdmin"] is True


# ── 3. 服务端才是真正的闸门（前端隐藏只是防误触） ──────────────────


def test_model_config_endpoints_reject_ordinary_accounts(monkeypatch):
    """普通账号访问模型配置接口一律 403 —— 前端藏起来不等于服务端放行。"""
    _isolate_admin_sources(monkeypatch)
    monkeypatch.setattr(settings, "invite_tokens", "")
    with TestClient(app) as client:
        _register_ordinary_user(client)
        for method, path in (
            ("get", "/api/settings/llm-configs"),
            ("get", "/api/settings/llm-health"),
            ("get", "/api/settings/role-profiles"),
        ):
            response = getattr(client, method)(path)
            assert response.status_code == 403, f"{path} -> {response.status_code}"
            assert "无权" in response.json()["detail"]


def test_model_config_endpoints_allow_an_admin(monkeypatch):
    """管理员（数据库标记）可以读模型配置 —— 与前端显示管理入口的条件一致。"""
    _isolate_admin_sources(monkeypatch)
    monkeypatch.setattr(settings, "invite_tokens", "")
    uid = users_dao.create_user(_name("admin-gate"), "strong-pass-1", is_admin=True)
    username = users_dao.get_user(uid)["username"]
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login", json={"username": username, "password": "strong-pass-1", "termsAccepted": True, "termsVersion": TERMS_VERSION}
        ).status_code == 200
        assert client.get("/api/auth/permissions").json()["data"]["isAdmin"] is True
        assert client.get("/api/settings/llm-configs").status_code == 200
