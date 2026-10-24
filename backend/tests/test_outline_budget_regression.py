"""Outline editing stays bounded after repeated arc splits (no network)."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.models.schemas import ChatRequest
from app.services import ai_service
from app.services.outline_candidate_service import build_outline_edit_context


def many_arcs(count=180):
    nodes = []
    for volume in range(1, 4):
        nodes.append({"id": f"volume-{volume}", "type": "volume", "order": volume,
                      "title": f"第{volume}卷", "summary": "卷内独立小故事。" * 80})
    for index in range(count):
        volume = index // (count // 3) + 1
        nodes.extend([
            {"id": f"arc-{index:012d}", "type": "arc", "parentId": f"volume-{volume}",
             "title": f"日常故事{index}", "summary": f"独立事件{index}：" + "起因经过与结果。" * 70,
             "order": index + 1},
            {"id": f"chapter-{index}", "type": "chapter", "parentId": f"volume-{volume}",
             "arcId": f"arc-{index:012d}", "title": f"第{index + 1}章", "order": index + 1},
        ])
    return {"mainSummary": "全书主线。" * 300, "nodes": nodes}


def test_large_outline_focus_keeps_story_details_and_never_mutates_source():
    outline = many_arcs()
    original = deepcopy(outline)
    context = build_outline_edit_context(outline, "调整第1卷的日常故事0，保留小故事的独立性")
    assert len(context) <= 9000
    assert "独立事件0" in context
    assert "volume-3" in context
    assert "chapter-150" not in context
    assert outline == original


@pytest.mark.asyncio
async def test_repeated_large_outline_edits_reach_provider_with_latest_command(monkeypatch):
    project = {"outline": many_arcs()}
    monkeypatch.setattr(ai_service.projects_dao, "get_project", lambda _: project)
    monkeypatch.setattr(ai_service.chapters_dao, "list_chapters", lambda _: [])
    monkeypatch.setattr(ai_service.project_context_service, "assemble_project_context", lambda **_: {
        "profile": "plot_planning", "text": "无关通用上下文" * 1000,
        "chars": 7000, "budget": 7000, "blocks": [],
    })
    provider = type("Provider", (), {"provider": "mock", "model": "mock"})()
    provider.chat = AsyncMock(return_value='{"patchMode":"merge","upsertNodes":[]}')
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: provider)
    history = []
    for turn in range(3):
        instruction = f"调整第1卷大纲中的日常故事0，第{turn}次修改要求：" + "保留因果关系。" * 160
        history.append({"role": "user", "content": instruction})
        result = await ai_service.chat_reply(ChatRequest(projectId="large", messages=history))
        assert result["mode"] == "llm", result
        sent = provider.chat.await_args.args[0]
        assert sum(len(m["content"]) for m in sent) <= 14000
        assert sent[-1]["content"] == instruction
        assert "独立事件0" in "\n".join(m["content"] for m in sent)
        history.append({"role": "assistant", "content": "历史大纲回复" * 3000})
    assert provider.chat.await_count == 3


def test_large_outline_patch_apply_and_next_edit_preserve_navigation(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app, _request_windows

    sent_sizes = []
    class Provider:
        provider = model = "mock"

        async def chat(self, messages, **_kwargs):
            sent_sizes.append(sum(len(m["content"]) for m in messages))
            return json.dumps({"patchMode": "merge", "upsertNodes": [{
                "id": "arc-000000000000", "summary": f"作者第{len(sent_sizes)}次确认的新日常故事。",
            }]}, ensure_ascii=False)

    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()
    outline = many_arcs()
    with TestClient(app) as client:
        created = client.post("/api/projects/", json={"name": "多轮编辑隔离测试"})
        pid = created.json()["data"]["id"]
        assert client.patch(f"/api/projects/{pid}", json={"outline": outline}).status_code == 200
        for turn in range(1, 4):
            response = client.post("/api/ai/chat", json={"projectId": pid, "messages": [
                {"role": "user", "content": "请调整第1卷大纲的日常故事0"},
            ]})
            data = response.json()["data"]
            assert data["mode"] == "llm", data
            candidate = data["candidate"]
            assert candidate["status"] == "proposed"
            before = client.get(f"/api/projects/{pid}").json()["data"]["outline"]
            assert before.get("outlineVersion", 1) == turn
            applied = client.post(f"/api/outline-candidates/{candidate['id']}/apply")
            assert applied.status_code == 200, applied.text
            current = client.get(f"/api/projects/{pid}").json()["data"]["outline"]
            assert current["outlineVersion"] == turn + 1
            assert {n["id"] for n in current["nodes"]} == {n["id"] for n in outline["nodes"]}
            chapters = [n for n in current["nodes"] if n["type"] == "chapter"]
            assert len(chapters) == 180
    assert len(sent_sizes) == 3 and max(sent_sizes) <= 14000
