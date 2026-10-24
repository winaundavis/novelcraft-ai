"""登录不得被普通流量挤成 429（复现"刷新几次后登录显示请求过于频繁"）。

背景：`main.py` 的 IP 限流窗口默认 30 次/60 秒（`RATE_LIMIT`，见 `.env.example`）。
Vite 把 `/api` 代理到后端，因此浏览器所有请求共用 `127.0.0.1` 这一个桶。
工作区一次加载会发出多个非豁免读请求（项目、设置、伏笔、投影、候选…），
消耗完窗口后，**登录本身**也会返回 429，用户彻底进不去。

既有设计**刻意**让只读请求占用配额（见 `test_phase10a_security.py`：
"Project-list reads must consume the normal production request quota"），
因此本修复不豁免读请求，而是把登录/注册放进**独立的 IP 窗口**：
既不挤占通用配额，也保留对暴力破解的限制。
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from main import app

BAD_LOGIN = {"username": "nobody", "password": "wrong-pass-1"}


def _reset_windows() -> None:
    import main

    main._request_windows.clear()
    getattr(main, "_auth_windows", {}).clear()


def test_login_is_not_starved_by_project_reads(monkeypatch):
    """通用窗口被读请求用满后，登录仍必须可达（不能被拖成 429）。"""
    _reset_windows()
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    monkeypatch.setattr(settings, "rate_limit", 3)

    with TestClient(app) as client:
        reads = [client.get("/api/projects/").status_code for _ in range(3)]
        assert reads == [401, 401, 401], reads
        # 通用窗口已满——这是既有且被测试固定的设计
        assert client.get("/api/projects/").status_code == 429

        login = client.post("/api/auth/login", json=BAD_LOGIN)

    assert login.status_code != 429, f"登录被通用窗口拖累: {login.status_code} {login.text}"
    assert login.status_code == 401


def test_auth_attempts_do_not_consume_the_general_quota(monkeypatch):
    """登录尝试不应占用通用窗口，否则会挤掉用户自己的正常读请求。"""
    _reset_windows()
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    monkeypatch.setattr(settings, "rate_limit", 3)

    with TestClient(app) as client:
        for _ in range(3):
            client.post("/api/auth/login", json=BAD_LOGIN)
        first_read = client.get("/api/projects/").status_code

    assert first_read == 401, f"登录尝试挤占了通用配额: {first_read}"


def test_repeated_login_attempts_are_still_limited(monkeypatch):
    """独立窗口仍要限制暴力破解：连续同 IP 登录尝试最终必须 429。"""
    _reset_windows()
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    monkeypatch.setattr(settings, "rate_limit", 2)

    with TestClient(app) as client:
        codes = [client.post("/api/auth/login", json=BAD_LOGIN).status_code for _ in range(4)]

    assert codes[:2] == [401, 401], codes
    assert codes[2] == 429 and codes[3] == 429, codes
