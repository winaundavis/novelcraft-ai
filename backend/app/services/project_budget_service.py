"""Atomic, SQLite-backed daily project LLM limits."""

from contextlib import closing

from app.core.config import settings
from app.db import get_conn, utc_today
from app.llm.audit import get_llm_audit_context
from app.llm.provider import LLMBudgetExceededError


def consume_project_budget(estimated_tokens: int) -> None:
    project_id = str(get_llm_audit_context().get("project_id") or "")
    if not project_id:
        return
    today = utc_today()
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_llm_usage (project_id TEXT NOT NULL, usage_date TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project_id, usage_date))"
        )
        # Serialize the read/check/write across threads and independent SQLite
        # connections. A deferred SELECT followed by UPSERT can spend the same
        # last slot several times even with a single application worker.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT calls, tokens FROM project_llm_usage WHERE project_id = ? AND usage_date = ?", (project_id, today)).fetchone()
        calls, tokens = (int(row["calls"]), int(row["tokens"])) if row else (0, 0)
        if calls + 1 > settings.project_daily_llm_call_limit or tokens + estimated_tokens > settings.project_daily_token_limit:
            raise LLMBudgetExceededError("今日项目 AI 使用额度已达到上限。")
        conn.execute(
            "INSERT INTO project_llm_usage(project_id, usage_date, calls, tokens) VALUES (?, ?, 1, ?) ON CONFLICT(project_id, usage_date) DO UPDATE SET calls = calls + 1, tokens = tokens + excluded.tokens",
            (project_id, today, estimated_tokens),
        )


def settle_project_budget(estimated_tokens: int, actual_tokens: int) -> None:
    """Replace this request's conservative token reservation with provider usage.

    Call count remains project/date scoped. Unknown or zero provider usage keeps the
    reservation intact so a failed request cannot silently bypass the cost guard.
    """
    project_id = str(get_llm_audit_context().get("project_id") or "")
    estimate = max(0, int(estimated_tokens))
    actual = max(0, int(actual_tokens))
    if not project_id or estimate <= 0 or actual <= 0:
        return
    delta = actual - estimate
    if delta == 0:
        return
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE project_llm_usage SET tokens = MAX(0, tokens + ?) WHERE project_id = ? AND usage_date = ?",
            (delta, project_id, utc_today()),
        )
