"""Simultaneous reservations must never spend the last project slot twice."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from app.core.config import settings
from app.llm.audit import set_llm_audit_context, reset_llm_audit_context
from app.llm.provider import LLMBudgetExceededError
from app.services import project_budget_service as service


@pytest.mark.parametrize("call_limit,token_limit", [(1, 10000), (100, 10)])
def test_concurrent_reservations_do_not_exceed_either_limit(tmp_path, monkeypatch, call_limit, token_limit):
    database = tmp_path / "budget.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE project_llm_usage (project_id TEXT NOT NULL, usage_date TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project_id,usage_date))")
    start = Barrier(6)
    # A SELECT outside a transaction can race with any peer. Synchronize this
    # window to deterministically reproduce stale reads in the old implementation.
    read_window = Barrier(6)

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith("SELECT calls") and not self.in_transaction:
                rows = cursor.fetchall()
                read_window.wait(timeout=10)
                class Snapshot:
                    def fetchone(self):
                        return rows[0] if rows else None
                return Snapshot()
            return cursor

    def connect():
        conn = sqlite3.connect(database, factory=Connection, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(service, "get_conn", connect)
    monkeypatch.setattr(settings, "project_daily_llm_call_limit", call_limit)
    monkeypatch.setattr(settings, "project_daily_token_limit", token_limit)

    def reserve(_):
        token = set_llm_audit_context(project_id="synthetic-project", merge=False)
        try:
            start.wait(timeout=10)
            try:
                service.consume_project_budget(10)
                return True
            except LLMBudgetExceededError:
                return False
        finally:
            reset_llm_audit_context(token)

    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = list(pool.map(reserve, range(6)))
    assert sum(accepted) == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT calls,tokens FROM project_llm_usage").fetchone() == (1, 10)
