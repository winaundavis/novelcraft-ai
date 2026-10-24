"""Phase 11-A zero-API operational isolation contracts."""

from unittest.mock import AsyncMock
import sqlite3
import uuid

import pytest

from app.llm import audit as audit_mod
from app.llm.resolver import get_runtime_provider, reset_runtime_provider, set_runtime_provider
from app.services import ai_service
from app.services import run_context
from app.services import beta_smoke_manifest_service


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    path = tmp_path / "phase11a-audit.db"
    monkeypatch.setattr(audit_mod, "DB_PATH", path)
    audit_mod._ensure_table()
    audit_mod.clear_llm_audit_context()
    audit_mod.clear_llm_call_meta()
    run_context.clear_run()
    yield path
    run_context.clear_run()


class _Provider:
    def __init__(self, value: str):
        self.value = value

    async def chat(self, *_args, **_kwargs):
        return self.value


def test_provider_resolver_switches_current_provider():
    first = _Provider("mock-A")
    second = _Provider("mock-B")
    set_runtime_provider(first)
    assert get_runtime_provider() is first
    set_runtime_provider(second)
    assert get_runtime_provider() is second
    set_runtime_provider(None)


@pytest.mark.asyncio
async def test_ai_service_uses_runtime_provider_override(monkeypatch):
    provider = _Provider("mock-A")
    token = set_runtime_provider(provider)
    monkeypatch.setattr(
        ai_service.project_context_service,
        "assemble_project_context",
        lambda **_kwargs: {"text": "context", "profile": "", "chars": 1, "budget": 1, "blocks": []},
    )
    try:
        result = await ai_service.chat_reply(
            ai_service.ChatRequest(projectId="p1", messages=[{"role": "user", "content": "hi"}])
        )
    finally:
        reset_runtime_provider(token)
    assert result["reply"] == "mock-A"


def test_default_audit_query_is_scoped_to_current_run(audit_db):
    run_context.start_run("run-a")
    audit_mod.record_llm_call(provider="mock-A", model="m", source="llm")
    run_context.start_run("run-b")
    audit_mod.record_llm_call(provider="mock-B", model="m", source="llm")
    assert [row["provider"] for row in audit_mod.query_llm_audit(minutes=10)] == ["mock-B"]
    run_context.clear_run()
    assert len(audit_mod.query_llm_audit(minutes=10, run_id="run-a")) == 1


def test_run_manifest_is_secret_free(tmp_path):
    run_context.start_run("manifest-run")
    path = tmp_path / "run.json"
    manifest = run_context.write_run_manifest(
        path,
        provider="arts",
        model="deepseek-v4-flash",
        temperature=0.8,
        max_tokens=4096,
        thinking={"type": "disabled"},
        reasoning="none",
        environment="beta",
        database="sqlite",
    )
    assert manifest["run_id"] == "manifest-run"
    assert manifest["provider"] == "arts"
    assert "api_key" not in path.read_text(encoding="utf-8").lower()


def test_beta_manifest_resolves_runtime_provider(monkeypatch):
    calls = []
    class Provider:
        def request_manifest(self, role):
            calls.append(role)
            return {"provider": "mock-A", "model": "m"}
    monkeypatch.setattr(beta_smoke_manifest_service.projects_dao, "get_project", lambda _: {"styleConfig": {}})
    monkeypatch.setattr(beta_smoke_manifest_service.chapters_dao, "get_chapter", lambda *_: {"id": "c1", "title": "x"})
    monkeypatch.setattr(beta_smoke_manifest_service.chapter_contracts_dao, "get_latest_contract", lambda *_: {})
    token = set_runtime_provider(Provider())
    try:
        result = beta_smoke_manifest_service.build_manifest("p1", "c1")
    finally:
        reset_runtime_provider(token)
    assert result["provider"] == "mock-A"
    assert calls


@pytest.mark.asyncio
async def test_provider_and_run_context_are_isolated_concurrently(audit_db):
    async def worker(run_id, provider):
        token = set_runtime_provider(provider)
        run_context.start_run(run_id)
        await __import__("asyncio").sleep(0)
        assert get_runtime_provider() is provider
        audit_mod.record_llm_call(provider=provider.value, model="m", source="llm")
        rows = audit_mod.query_llm_audit(minutes=10)
        reset_runtime_provider(token)
        return rows

    first, second = await __import__("asyncio").gather(
        worker("run-A", _Provider("A")), worker("run-B", _Provider("B"))
    )
    assert [row["provider"] for row in first] == ["A"]
    assert [row["provider"] for row in second] == ["B"]


def test_project_daily_budget_is_shared_across_runs(monkeypatch):
    from app.core.config import settings
    from app.db import get_conn, init_db
    from app.services.project_budget_service import consume_project_budget
    from app.llm.provider import LLMResponseError
    project_id = "phase11b-budget-shared-" + uuid.uuid4().hex
    init_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO projects (id, owner_id, name, created_at, updated_at, style_config) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, "local-dev", "预算隔离", "2026-01-01", "2026-01-01", "{}"),
        )
    monkeypatch.setattr(settings, "project_daily_llm_call_limit", 2)
    monkeypatch.setattr(settings, "project_daily_token_limit", 100)
    for run_id in ("run-A", "run-B"):
        run_context.start_run(run_id)
        audit_mod.set_llm_audit_context(project_id=project_id, merge=False)
        consume_project_budget(10)
    with pytest.raises(LLMResponseError):
        consume_project_budget(10)
    audit_mod.clear_llm_audit_context()
    run_context.clear_run()


def test_internal_health_requires_maintainer(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from app.core.config import settings
    monkeypatch.setattr(settings, "debug", False)
    assert TestClient(app).get("/api/health/internal").status_code == 401
