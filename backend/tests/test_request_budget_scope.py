"""Validated HTTP project identity must reach every provider budget gate."""

import asyncio
import json

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.core.auth import require_project_owner
from app.core.config import settings
from app.db import get_conn, utc_today
from app.llm.audit import get_llm_audit_context, reset_llm_audit_context, set_llm_audit_context
from app.llm.provider import LLMProvider
from main import app

ASGIClient = httpx.AsyncClient

@pytest.fixture
def budget_client(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "scope-alpha:scope-user")
    monkeypatch.setattr(settings, "rate_limit", 1000)
    monkeypatch.setattr(settings, "project_daily_llm_call_limit", 0)
    monkeypatch.setattr("app.llm.provider.get_active_llm_config", lambda: None)
    provider = LLMProvider(provider="arts", model="synthetic", api_key="synthetic")
    transport_calls = []

    class FakeHTTP:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            transport_calls.append(get_llm_audit_context())
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "choices": [{"message": {"content": '{"issues":[],"summary":"synthetic"}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            })

    monkeypatch.setattr("app.llm.provider.httpx.AsyncClient", FakeHTTP)
    # Legacy services still resolve the shared provider directly; preserve the
    # real provider implementation while replacing only network transport.
    for module in ("review_service", "deai_service", "fulltext_service", "foreshadow_service", "ai_service"):
        monkeypatch.setattr(f"app.services.{module}.llm", provider)
    monkeypatch.setattr("app.services.style_engine.extraction.llm", provider)
    with TestClient(app, headers={"X-NovelCraft-Invite": "scope-alpha"}) as client:
        created = client.post("/api/projects/", json={"name": "Synthetic scope", "genre": "scifi"})
        assert created.status_code == 200
        project = created.json()["data"]["id"]
        chapter = client.post(f"/api/projects/{project}/chapters/", json={"title": "Synthetic", "volumeId": "", "order": 1}).json()["data"]["id"]
        assert client.put(f"/api/projects/{project}/chapters/{chapter}", json={"content": "Synthetic text. " * 50}).status_code == 200
        assert client.patch(f"/api/projects/{project}", json={"outline": {"mainSummary": "Synthetic", "nodes": [{"id": chapter, "type": "chapter", "title": "Synthetic", "order": 1}]}}).status_code == 200
        yield client, project, chapter, transport_calls


@pytest.mark.parametrize("operation", ["review", "deai", "fulltext", "style", "foreshadow", "enhance", "continuation"])
def test_exhausted_project_blocks_all_http_model_entries(budget_client, operation):
    """Removing validated project scope must not let a fallback hide paid HTTP."""
    client, project, chapter, calls = budget_client
    routes = {
        "review": ("/api/review/comprehensive", {"projectId": project, "chapterId": chapter, "text": "Synthetic text."}),
        "deai": ("/api/ai/deai", {"projectId": project, "text": "x" * 16001}),
        "fulltext": (f"/api/projects/{project}/fulltext-ask", {"question": "Synthetic?"}),
        "style": (f"/api/projects/{project}/style-analysis", None),
        "foreshadow": (f"/api/projects/{project}/foreshadows/suggest", None),
        "enhance": ("/api/ai/enhance", {"projectId": project, "text": "Synthetic text."}),
        "continuation": ("/api/ai/continue-writing", {"projectId": project, "chapterId": chapter, "contextBefore": "Synthetic text."}),
    }
    path, body = routes[operation]
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert calls == [], "exhausted project reached provider HTTP"
    with get_conn() as conn:
        row = conn.execute("SELECT calls FROM project_llm_usage WHERE project_id=? AND usage_date=?", (project, utc_today())).fetchone()
    assert row is None


def test_project_quota_is_shared_across_entries_and_does_not_block_another_project(budget_client, monkeypatch):
    """The fix must debit the verified project, rather than block every call."""
    client, project, chapter, calls = budget_client
    monkeypatch.setattr(settings, "project_daily_llm_call_limit", 1)
    first = client.post(f"/api/projects/{project}/fulltext-ask", json={"question": "Synthetic?"})
    assert first.status_code == 200
    assert not first.json()["data"]["degraded"]
    assert len(calls) == 1
    assert calls[0]["project_id"] == project
    second = client.post("/api/ai/deai", json={"projectId": project, "text": "Synthetic text."})
    assert second.status_code == 200
    assert second.json()["data"]["degraded"]
    assert len(calls) == 1
    other = client.post("/api/projects/", json={"name": "Other scope", "genre": "scifi"}).json()["data"]["id"]
    third = client.post("/api/ai/enhance", json={"projectId": other, "text": "Synthetic text."})
    assert third.status_code == 200
    assert len(calls) == 2
    assert calls[1]["project_id"] == other
    with get_conn() as conn:
        rows = conn.execute("SELECT project_id,calls,tokens FROM project_llm_usage WHERE project_id IN (?,?) AND usage_date=?", (project, other, utc_today())).fetchall()
    assert {row["project_id"]: (row["calls"], row["tokens"]) for row in rows} == {project: (1, 20), other: (1, 20)}


def test_verified_owner_binds_scope_but_failed_owner_does_not(monkeypatch):
    """A caller's unverified project choice must never replace billing scope."""
    monkeypatch.setattr("app.core.auth.current_user_id", lambda request: "owner")
    monkeypatch.setattr("app.core.auth.projects_dao.get_project_owner", lambda project: "owner" if project == "owned" else "other")
    monkeypatch.setattr("app.core.auth.is_admin_user", lambda user: False)
    token = set_llm_audit_context(project_id="outer", merge=False)
    try:
        request = Request({"type": "http", "headers": []})
        require_project_owner(request, "owned")
        assert get_llm_audit_context()["project_id"] == "owned"
        with pytest.raises(HTTPException) as rejected:
            require_project_owner(request, "foreign")
        assert rejected.value.status_code == 403
        assert get_llm_audit_context()["project_id"] == "owned"
    finally:
        reset_llm_audit_context(token)


@pytest.mark.asyncio
async def test_http_scope_survives_stream_and_background_without_request_leak(monkeypatch):
    """Request reset must retain child work while isolating concurrent requests."""
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr("app.core.auth.projects_dao.get_project_owner", lambda project: "local-dev")
    captured = []
    seen_projects = set()
    both_entered = asyncio.Event()

    async def endpoint(project_id: str, request: Request, background_tasks: BackgroundTasks):
        require_project_owner(request, project_id)
        seen_projects.add(project_id)
        if len(seen_projects) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 5)

        async def background():
            captured.append((project_id, get_llm_audit_context().get("project_id")))

        background_tasks.add_task(background)

        async def stream():
            await asyncio.sleep(0)
            yield json.dumps({"project": get_llm_audit_context().get("project_id")})

        return StreamingResponse(stream(), background=background_tasks)

    async def unscoped():
        return get_llm_audit_context()

    start = len(app.router.routes)
    app.add_api_route("/_scope_probe/{project_id}", endpoint)
    app.add_api_route("/_scope_probe", unscoped)
    token = set_llm_audit_context(project_id="ambient", job_id="ambient-job", merge=False)
    try:
        async with ASGIClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            results = await asyncio.gather(client.get("/_scope_probe/first"), client.get("/_scope_probe/second"))
            assert [r.json()["project"] for r in results] == ["first", "second"]
            assert sorted(captured) == [("first", "first"), ("second", "second")]
            assert (await client.get("/_scope_probe")).json().get("project_id", "") == ""
        assert get_llm_audit_context()["project_id"] == "ambient"
    finally:
        reset_llm_audit_context(token)
        del app.router.routes[start:]
