"""非管理员每日「按大纲自动创作」章数额度。

额度口径（产品决定）：
* 只有「按大纲自动创作」**成功落库**的章计入额度；
* 改写、审校、续写、大纲、人物、对话等操作不占用额度；
* 管理员不限额；生产环境必须配置 >0 的上限。
"""

from __future__ import annotations

import pytest

from app.core.config import production_config_errors, settings
from app.db import get_conn
from app.services import novel_generation_service as ngs
from app.services import user_quota_service as quota


@pytest.fixture(autouse=True)
def _isolated_usage():
    with get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_daily_chapter_usage ("
            "user_id TEXT NOT NULL, usage_date TEXT NOT NULL, "
            "chapters INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, usage_date))"
        )
        conn.execute("DELETE FROM user_daily_chapter_usage")
    yield


@pytest.fixture
def normal_user(monkeypatch):
    monkeypatch.setattr(settings, "user_daily_chapter_limit", 10)
    monkeypatch.setattr(quota, "_is_admin", lambda _user_id: False)
    return "user-normal"


@pytest.fixture
def admin_user(monkeypatch):
    monkeypatch.setattr(settings, "user_daily_chapter_limit", 10)
    monkeypatch.setattr(quota, "_is_admin", lambda _user_id: True)
    return "user-admin"


def test_default_quota_starts_at_ten(normal_user):
    assert quota.used_chapters(normal_user) == 0
    assert quota.remaining_chapters(normal_user) == 10


def test_consuming_chapters_reduces_remaining(normal_user):
    assert quota.consume_chapter(normal_user) == 1
    assert quota.consume_chapter(normal_user) == 2
    assert quota.consume_chapter(normal_user, amount=8) == 10
    assert quota.used_chapters(normal_user) == 10
    assert quota.remaining_chapters(normal_user) == 0


def test_admin_is_unlimited(admin_user):
    assert quota.is_unlimited(admin_user) is True
    assert quota.remaining_chapters(admin_user) is None


def test_promoted_admin_becomes_unlimited(normal_user, monkeypatch):
    quota.consume_chapter(normal_user, amount=10)
    assert quota.remaining_chapters(normal_user) == 0
    monkeypatch.setattr(quota, "_is_admin", lambda _user_id: True)
    assert quota.remaining_chapters(normal_user) is None


def test_zero_limit_disables_quota_outside_production(normal_user, monkeypatch):
    monkeypatch.setattr(settings, "user_daily_chapter_limit", 0)
    assert quota.remaining_chapters(normal_user) is None


def test_switch_disables_quota_for_normal_user(normal_user, monkeypatch):
    """灰度开关关闭时，非管理员也不受限——这是小范围测试期的目标状态。"""
    monkeypatch.setattr(settings, "user_daily_chapter_limit_enabled", False)
    assert quota.quota_enabled() is False
    assert quota.is_unlimited(normal_user) is True
    assert quota.remaining_chapters(normal_user) is None
    # 额度数字本身保持不变，正式上线只需把开关改回 true
    assert quota.chapter_limit() == 10


def test_switch_back_on_restores_limit(normal_user, monkeypatch):
    monkeypatch.setattr(settings, "user_daily_chapter_limit_enabled", False)
    assert quota.remaining_chapters(normal_user) is None
    monkeypatch.setattr(settings, "user_daily_chapter_limit_enabled", True)
    assert quota.remaining_chapters(normal_user) == 10


def test_switch_off_keeps_usage_accounting_intact(normal_user, monkeypatch):
    """关闭开关只是不拦截，不应清空或伪造已用量。"""
    quota.consume_chapter(normal_user, amount=4)
    monkeypatch.setattr(settings, "user_daily_chapter_limit_enabled", False)
    assert quota.used_chapters(normal_user) == 4


@pytest.mark.parametrize(
    "remaining,expected,takes_notice",
    [(None, 10, False), (10, 10, False), (3, 3, True), (1, 1, True)],
)
def test_apply_user_quota_clamps_task(normal_user, monkeypatch, remaining, expected, takes_notice):
    nodes = [{"id": f"c{index}", "title": f"第{index}章"} for index in range(1, 11)]
    monkeypatch.setattr(quota, "remaining_chapters", lambda _user_id, **_kwargs: remaining)
    kept, notice = ngs._apply_user_quota(nodes, normal_user)
    assert kept == nodes[:expected]
    assert ("剩余" in notice) is takes_notice


def test_apply_user_quota_rejects_when_exhausted(normal_user, monkeypatch):
    nodes = [{"id": "c1", "title": "第一章"}]
    monkeypatch.setattr(quota, "remaining_chapters", lambda _user_id, **_kwargs: 0)
    with pytest.raises(RuntimeError) as excinfo:
        ngs._apply_user_quota(nodes, normal_user)
    message = str(excinfo.value)
    assert "额度已用完" in message
    # 必须明确告知改写/审校不受影响，避免用户以为整个 AI 功能被锁
    assert "改写" in message and "审校" in message


def test_production_rejects_zero_chapter_limit(monkeypatch):
    monkeypatch.setattr(settings, "environment", "beta")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "cors_allow_origins", "https://example.test")
    monkeypatch.setattr(settings, "allowed_hosts", "example.test")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "admin_user_ids", "user-1")
    monkeypatch.setattr(settings, "user_daily_chapter_limit", 0)
    errors = production_config_errors(settings)
    assert "quota_invalid" in errors


def test_production_accepts_positive_chapter_limit(monkeypatch):
    monkeypatch.setattr(settings, "environment", "beta")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "cors_allow_origins", "https://example.test")
    monkeypatch.setattr(settings, "allowed_hosts", "example.test")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "admin_user_ids", "user-1")
    monkeypatch.setattr(settings, "user_daily_chapter_limit", 10)
    errors = production_config_errors(settings)
    assert "quota_invalid" not in errors


def test_production_allows_zero_limit_when_switch_is_off(monkeypatch):
    """灰度关闭时允许限额为 0（此时 0 无意义），不应阻断生产就绪。"""
    monkeypatch.setattr(settings, "environment", "beta")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "cors_allow_origins", "https://example.test")
    monkeypatch.setattr(settings, "allowed_hosts", "example.test")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "admin_user_ids", "user-1")
    monkeypatch.setattr(settings, "user_daily_chapter_limit", 0)
    monkeypatch.setattr(settings, "user_daily_chapter_limit_enabled", False)
    errors = production_config_errors(settings)
    assert "quota_invalid" not in errors
