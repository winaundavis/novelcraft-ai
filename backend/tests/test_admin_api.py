"""运营后台 API 测试。

重点验证三件事：
1) 未登录 401、普通用户 403、管理员 200 —— 权限隔离不能靠前端隐藏。
2) 统计口径正确：`project_llm_usage` → 按用户/按日/按项目聚合。
3) 邀请码创建与停用可用，且明文只出现在创建响应里。
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.dao import invites as invites_dao
from app.dao import users
from app.db import get_conn, utc_today


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def admin_session(monkeypatch):
    from fastapi.testclient import TestClient

    from main import _request_windows, app

    user_id = users.create_user(_unique("admin"), "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"probe-admin:{user_id}")
    monkeypatch.setattr(settings, "admin_user_ids", user_id)
    monkeypatch.setattr(settings, "debug", False)
    _request_windows.clear()
    with TestClient(app) as client:
        yield client, {"X-NovelCraft-Invite": "probe-admin"}, user_id
    _request_windows.clear()


@pytest.fixture
def normal_session(monkeypatch):
    from fastapi.testclient import TestClient

    from main import _request_windows, app

    user_id = users.create_user(_unique("normal"), "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"probe-normal:{user_id}")
    monkeypatch.setattr(settings, "admin_user_ids", "")
    monkeypatch.setattr(settings, "debug", False)
    _request_windows.clear()
    with TestClient(app) as client:
        yield client, {"X-NovelCraft-Invite": "probe-normal"}, user_id
    _request_windows.clear()


def _reset_usage() -> None:
    with get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_llm_usage ("
            "project_id TEXT NOT NULL, usage_date TEXT NOT NULL, "
            "calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(project_id, usage_date))"
        )
        conn.execute("DELETE FROM project_llm_usage")


def _create_project(client, headers) -> str:
    created = client.post(
        "/api/projects/",
        json={"name": "后台测试项目", "genre": "xuanhuan", "targetWordCount": 1000, "stylePreset": "epic"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    return created.json()["data"]["id"]


ADMIN_GET_PATHS = ("/api/admin/overview", "/api/admin/users", "/api/admin/usage", "/api/admin/invites")


@pytest.mark.parametrize("path", ADMIN_GET_PATHS)
def test_admin_endpoints_reject_anonymous(path, monkeypatch):
    """生产口径（DEBUG=false）下，未带任何凭据必须 401。

    注意：DEBUG=true 时 `current_user_id` 会返回 `local-dev`，而它默认在
    `ADMIN_USER_IDS` 里 —— 也就是说**调试模式等于开放管理员权限**。
    这条测试固定的是生产口径，防止有人误以为默认就是安全的。
    """
    from fastapi.testclient import TestClient

    from main import _request_windows, app

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "admin_user_ids", "someone-else")
    monkeypatch.setattr(settings, "invite_tokens", "")
    _request_windows.clear()
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
    _request_windows.clear()


def test_debug_mode_anonymous_is_admin_by_design(monkeypatch):
    """记录既有行为：DEBUG=true 且默认 ADMIN_USER_IDS 时匿名即管理员。

    生产必须保持 DEBUG=false —— 这条测试的存在本身就是为了让这个前提可见。
    """
    from fastapi.testclient import TestClient

    from main import _request_windows, app

    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "admin_user_ids", "local-dev")
    _request_windows.clear()
    with TestClient(app) as client:
        assert client.get("/api/admin/overview").status_code == 200
    _request_windows.clear()


@pytest.mark.parametrize("path", ADMIN_GET_PATHS)
def test_admin_endpoints_reject_normal_user(path, normal_session):
    client, headers, _user_id = normal_session
    assert client.get(path, headers=headers).status_code == 403


def test_normal_user_cannot_create_or_disable_invites(normal_session):
    client, headers, _user_id = normal_session
    assert client.post("/api/admin/invites", json={"label": "x"}, headers=headers).status_code == 403
    assert client.post("/api/admin/invites/whatever/disable", headers=headers).status_code == 403


def test_overview_returns_monitoring_fields(admin_session):
    client, headers, _user_id = admin_session
    body = client.get("/api/admin/overview", headers=headers).json()["data"]
    assert set(body) >= {"readiness", "serverTimeUtc", "uptimeSeconds", "backup", "disk", "counts"}
    assert isinstance(body["readiness"]["ready"], bool)
    assert isinstance(body["readiness"]["errors"], list)
    assert body["uptimeSeconds"] >= 0
    assert set(body["counts"]) >= {"users", "projects", "chapters", "invitesPending", "invitesBound"}


def test_users_report_usage_without_leaking_content(admin_session):
    client, headers, user_id = admin_session
    _reset_usage()
    project_id = _create_project(client, headers)

    today = utc_today()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO project_llm_usage(project_id, usage_date, calls, tokens) VALUES (?,?,?,?)",
            (project_id, today, 7, 4321),
        )

    users_payload = client.get("/api/admin/users?days=7", headers=headers).json()["data"]["users"]
    row = next(item for item in users_payload if item["id"] == user_id)
    assert row["projectCount"] >= 1
    assert row["todayCalls"] == 7
    assert row["todayTokens"] == 4321
    assert row["windowCalls"] == 7
    assert row["isAdmin"] is True
    # 隐私红线：列表里不能出现作品正文相关字段
    assert not ({"content", "chapters", "preview"} & set(row))


def test_usage_aggregates_by_day_and_project(admin_session):
    client, headers, user_id = admin_session
    _reset_usage()
    project_id = _create_project(client, headers)
    today = utc_today()

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO project_llm_usage(project_id, usage_date, calls, tokens) VALUES (?,?,?,?)",
            (project_id, today, 3, 900),
        )

    body = client.get("/api/admin/usage?days=7", headers=headers).json()["data"]
    assert body["days"] == 7
    assert body["totals"]["calls"] >= 3
    assert body["totals"]["tokens"] >= 900
    assert any(item["date"] == today and item["calls"] >= 3 for item in body["byDay"])
    top = next(item for item in body["byProject"] if item["projectId"] == project_id)
    assert top["ownerId"] == user_id
    assert top["calls"] >= 3
    assert top["tokens"] >= 900


def test_usage_days_parameter_is_clamped(admin_session):
    client, headers, _user_id = admin_session
    assert client.get("/api/admin/usage?days=9999", headers=headers).json()["data"]["days"] == 90
    assert client.get("/api/admin/usage?days=0", headers=headers).json()["data"]["days"] == 1


def test_invite_create_then_disable(admin_session):
    client, headers, _user_id = admin_session
    created = client.post(
        "/api/admin/invites",
        json={"label": "后台测试", "maxUses": 1, "expiresInDays": 3},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()["data"]
    assert body["count"] == 1
    code = body["codes"][0]
    assert code and len(code) >= 8
    assert "只显示这一次" in body["warning"]

    listed = client.get("/api/admin/invites", headers=headers).json()["data"]["invites"]
    target = next(item for item in listed if item["label"] == "后台测试" and item["status"] == "pending")
    # 列表里只有 hint，不返回明文
    assert code not in str(listed)
    assert target["hint"] and len(target["hint"]) < len(code)

    disabled = client.post(f"/api/admin/invites/{target['id']}/disable", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "disabled"
    assert invites_dao.find_by_code(code)["status"] == "disabled"


def test_disable_unknown_invite_returns_404(admin_session):
    client, headers, _user_id = admin_session
    assert client.post("/api/admin/invites/not-exist/disable", headers=headers).status_code == 404


def test_batch_invite_creation_returns_all_codes(admin_session):
    """批量生成：一次拿回全部明文，且每个码都能被注册流程识别。"""
    client, headers, _user_id = admin_session
    created = client.post(
        "/api/admin/invites",
        json={"label": "批量测试", "count": 3, "maxUses": 1, "expiresInDays": 5},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()["data"]
    codes = body["codes"]
    assert body["count"] == 3
    assert len(codes) == 3
    assert len(set(codes)) == 3, "批量生成的码不能重复"
    for code in codes:
        row = invites_dao.find_by_code(code)
        assert row is not None and row["status"] == "pending"
        assert row["label"] == "批量测试"
    # 明文不在列表接口里回显
    listed = client.get("/api/admin/invites", headers=headers).json()["data"]["invites"]
    assert all(code not in str(listed) for code in codes)


@pytest.mark.parametrize("count", [0, 21])
@pytest.mark.parametrize("field", ["count"])
def test_batch_size_is_bounded(admin_session, count, field):
    """批量上限：防止一次生成过多导致明文刷屏与误操作。"""
    client, headers, _user_id = admin_session
    payload = {"label": "超限", field: count}
    assert client.post("/api/admin/invites", json=payload, headers=headers).status_code == 422
