"""非管理员每用户每日「按大纲自动创作」章数额度。

为什么不复用 `project_budget_service`：
* `project_llm_usage` 的维度是「项目 + UTC 日」，单位是「LLM 调用次数 / token」，
  它是**成本护栏**，防止一次异常刷爆账单。
* 产品需要的是「每个非管理员用户每天最多自动创作 10 章」，维度是「用户 + UTC 日」，
  单位是「章」。两者语义不同，不能互相替代：一次自动创作会消耗几十次 LLM 调用；
  而审校、改写、续写只消耗调用次数，按需求**不占用**章数额度。

因此本模块只统计「按大纲自动创作」成功落库的章数，不统计改写 / 审校 / 续写 /
大纲 / 人物 / 对话等操作；管理员完全不受限。
"""

from __future__ import annotations

from contextlib import closing

from app.core.config import settings
from app.db import get_conn, utc_today

_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS user_daily_chapter_usage ("
    "user_id TEXT NOT NULL, "
    "usage_date TEXT NOT NULL, "
    "chapters INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY(user_id, usage_date))"
)


def chapter_limit() -> int:
    """返回配置的每日章数上限；<=0 表示不启用（生产环境校验会拒绝该配置）。"""
    try:
        return int(getattr(settings, "user_daily_chapter_limit", 0) or 0)
    except (TypeError, ValueError):
        return 0


def quota_enabled() -> bool:
    """产品层额度是否启用。

    小范围测试期把 `USER_DAILY_CHAPTER_LIMIT_ENABLED` 设为 false 即可整体放开，
    正式上线前改回 true 恢复；额度数字本身不用动，避免上线时忘配或配错。
    """
    return bool(getattr(settings, "user_daily_chapter_limit_enabled", True))


def _is_admin(user_id: str) -> bool:
    # 延迟导入：避免 core.auth → dao → service 之间的导入顺序耦合。
    from app.core.auth import is_admin_user

    return bool(is_admin_user(user_id))


def is_unlimited(user_id: str) -> bool:
    """未启用额度 / 管理员 / 限额 <=0 / 空 user_id 均视为不受限。"""
    if not quota_enabled():
        return True
    if not user_id:
        return True
    if _is_admin(user_id):
        return True
    return chapter_limit() <= 0


def used_chapters(user_id: str, *, on_date: str | None = None) -> int:
    """今日已消费的自动创作章数。不存在记录时为 0。"""
    if not user_id:
        return 0
    today = on_date or utc_today()
    with closing(get_conn()) as conn, conn:
        conn.execute(_TABLE_SQL)
        row = conn.execute(
            "SELECT chapters FROM user_daily_chapter_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, today),
        ).fetchone()
        return int(row["chapters"]) if row else 0


def remaining_chapters(user_id: str, *, on_date: str | None = None) -> int | None:
    """今日剩余可自动创作章数；None 表示不受限。

    负剩余（历史超额）夹到 0，调用方只需判断 `<= 0` 即可拒绝。
    """
    if is_unlimited(user_id):
        return None
    return max(0, chapter_limit() - used_chapters(user_id, on_date=on_date))


def consume_chapter(user_id: str, amount: int = 1, *, on_date: str | None = None) -> int:
    """记录一章自动创作已成功落库，返回今日累计章数。

    这里**不**做上限判断：额度在任务启动时按剩余量裁剪，逐章只做记账。
    这样即使并发启动多个项目任务，也不会出现「同一份额度被反复花掉」。
    """
    if not user_id or amount <= 0:
        return 0
    today = on_date or utc_today()
    with closing(get_conn()) as conn, conn:
        conn.execute(_TABLE_SQL)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO user_daily_chapter_usage(user_id, usage_date, chapters) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, usage_date) DO UPDATE SET chapters = chapters + excluded.chapters",
            (user_id, today, int(amount)),
        )
        row = conn.execute(
            "SELECT chapters FROM user_daily_chapter_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, today),
        ).fetchone()
        return int(row["chapters"]) if row else 0


def clamp_to_quota(chapters: list, remaining: int | None) -> list:
    """把本次任务的章节列表裁剪到剩余额度；remaining 为 None 表示不裁剪。"""
    if remaining is None:
        return list(chapters)
    if remaining <= 0:
        return []
    return list(chapters[:remaining])


def quota_exhausted_message() -> str:
    limit = chapter_limit()
    return (
        f"今日自动创作额度已用完（{limit}/{limit} 章）。"
        "改写、审校、续写等操作不受影响；额度按 UTC 每日重置，明日可继续。"
    )


def quota_partial_message(remaining: int) -> str:
    return f"今日自动创作额度剩余 {remaining} 章，本次将先创作前 {remaining} 章。"
