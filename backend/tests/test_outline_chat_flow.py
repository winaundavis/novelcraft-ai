from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core.config import settings
from app.llm.roles import ROLE_PLANNING
from main import _request_windows, app


def _outline(summary: str) -> dict:
    return {
        "mainSummary": summary,
        "nodes": [
            {"id": "v1", "type": "volume", "parentId": None, "title": "第一卷", "summary": "旧卷", "order": 1},
            {"id": "a1", "type": "arc", "parentId": "v1", "title": "第一弧", "summary": "旧弧", "order": 1},
            {"id": "c1", "type": "chapter", "parentId": "v1", "arcId": "a1", "title": "第一章", "order": 1},
        ],
    }


def test_outline_edit_intent_accepts_volume_or_arc_edit_without_literal_outline_word():
    from app.services.ai_service import classify_chat_intent

    assert classify_chat_intent(
        [{"role": "user", "content": "请优化第一卷和各剧情弧的独立事件推进"}],
        None,
    ) == "outline_edit"
    assert classify_chat_intent(
        [{"role": "user", "content": "请介绍第一卷目前发生了什么"}],
        None,
    ) == "consultation"


def test_chapter_title_and_summary_edit_is_outline_mutation():
    from app.services.ai_service import classify_chat_intent

    assert classify_chat_intent(
        [{"role": "user", "content": "请优化第一章的标题和摘要，保持已有章节不变"}],
        None,
    ) == "outline_edit"


def test_outline_chat_uses_planning_role_and_creates_candidate_without_mutating_project(monkeypatch):
    from app.services import ai_service

    calls: list[str] = []

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, *, role=None, **_kwargs):
            calls.append(role)
            candidate = _outline("优化后的主线")
            candidate["nodes"][0]["summary"] = "第一卷经历完整的冲突、转折和结果。"
            return f"```json\n{json.dumps(candidate, ensure_ascii=False)}\n```"

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "测试", "premise": "旧故事"})
        project_id = created.json()["data"]["id"]
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": _outline("旧主线")})

        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化大纲，把第一卷写得更完整"}]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["candidate"]["status"] == "proposed"
        assert calls == [ROLE_PLANNING]
        current = client.get(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}).json()["data"]
        assert current["outline"]["mainSummary"] == "旧主线"


def test_legacy_flat_outline_chat_creates_candidate_for_existing_chapter(monkeypatch):
    from app.services import ai_service

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, *, role=None, **_kwargs):
            return json.dumps({
                "patchMode": "merge",
                "upsertNodes": [{
                    "id": "legacy-ch-1",
                    "title": "第一章 新标题",
                    "summary": "只修改当前历史章节，不创建卷或剧情弧。",
                }],
                "removeNodeIds": [],
            }, ensure_ascii=False)

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "旧版项目"})
        project_id = created.json()["data"]["id"]
        flat = {
            "mainSummary": "旧主线",
            "nodes": [
                {"id": "legacy-ch-1", "type": "chapter", "title": "第一章", "order": 1},
                {"id": "legacy-ch-2", "type": "chapter", "title": "第二章", "order": 2},
            ],
        }
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": flat})
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化第一章的标题和摘要"}]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["candidate"]["status"] == "proposed"


def test_outline_chat_rejects_zero_diff_candidate(monkeypatch):
    from app.services import ai_service

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, **_kwargs):
            return json.dumps({"patchMode": "merge", "upsertNodes": [], "removeNodeIds": []})

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "无变化项目"})
        project_id = created.json()["data"]["id"]
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": _outline("旧主线")})
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化大纲"}]},
        )

    data = response.json()["data"]
    assert data["mode"] == "failed"
    assert data["outline_failure_code"] == "outline_no_changes"
    assert "candidate" not in data


def test_outline_chat_marks_non_json_model_reply_invalid_instead_of_silent_success(monkeypatch):
    from app.services import ai_service

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, **_kwargs):
            return "我建议调整第一卷，但这里没有返回可应用的大纲结构。"

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    monkeypatch.setattr(ai_service, "get_llm_call_meta", lambda: {"audit_id": "audit-outline-1"})
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "测试"})
        project_id = created.json()["data"]["id"]
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": _outline("旧主线")})
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化大纲"}]},
        )

    data = response.json()["data"]
    assert data["mode"] == "failed"
    assert data["error_code"] == "outline_candidate_invalid"
    assert data["trace_id"] == "audit-outline-1"
    assert data["outline_failure_stage"] == "json_extract"
    assert data["outline_failure_code"] == "outline_json_missing"
    assert "candidate" not in data


def test_outline_chat_persists_postprocess_failure_on_the_provider_audit_row(tmp_path, monkeypatch):
    from app.llm import audit as audit_mod
    from app.services import ai_service

    audit_path = tmp_path / "outline-chat-audit.db"
    monkeypatch.setattr(audit_mod, "DB_PATH", audit_path)

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, **_kwargs):
            audit_id = audit_mod.record_llm_call(
                provider=self.provider,
                model=self.model,
                source="llm",
                status="success",
                http_status=200,
                finish_reason="stop",
            )
            audit_mod.set_llm_call_meta(
                provider=self.provider,
                model=self.model,
                audit_id=str(audit_id or ""),
                finish_reason="stop",
            )
            return "模型完成了分析，但没有返回结构化大纲补丁。"

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post(
            "/api/projects/",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"name": "测试"},
        )
        project_id = created.json()["data"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"outline": _outline("旧主线")},
        )
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={
                "projectId": project_id,
                "messages": [{"role": "user", "content": "请优化大纲"}],
            },
        )

    data = response.json()["data"]
    rows = audit_mod.query_llm_audit(minutes=10, scope="all")
    assert response.status_code == 200, response.text
    assert len(rows) == 1
    assert rows[0]["id"] == data["trace_id"]
    assert rows[0]["postprocess_status"] == "failed"
    assert rows[0]["postprocess_stage"] == "json_extract"
    assert rows[0]["postprocess_code"] == "outline_json_missing"
    assert "prompt" not in rows[0]
    assert "reply" not in rows[0]


def test_outline_chat_reports_candidate_persistence_failure_without_leaking_database_error(tmp_path, monkeypatch):
    from app.dao import outline_candidates as candidates_dao
    from app.llm import audit as audit_mod
    from app.services import ai_service

    audit_path = tmp_path / "outline-candidate-create-audit.db"
    monkeypatch.setattr(audit_mod, "DB_PATH", audit_path)

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, **_kwargs):
            audit_id = audit_mod.record_llm_call(
                provider=self.provider,
                model=self.model,
                source="llm",
                status="success",
                http_status=200,
                finish_reason="stop",
            )
            audit_mod.set_llm_call_meta(
                provider=self.provider,
                model=self.model,
                audit_id=str(audit_id or ""),
                finish_reason="stop",
            )
            return json.dumps({
                "patchMode": "merge",
                "upsertNodes": [{"id": "a1", "summary": "新的剧情弧"}],
                "removeNodeIds": [],
            }, ensure_ascii=False)

    def fail_candidate_create(**_kwargs):
        raise RuntimeError("database path and SQL must stay private")

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    monkeypatch.setattr(candidates_dao, "create_outline_candidate", fail_candidate_create)
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post(
            "/api/projects/",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"name": "测试"},
        )
        project_id = created.json()["data"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"outline": _outline("旧主线")},
        )
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={
                "projectId": project_id,
                "messages": [{"role": "user", "content": "请优化大纲第一弧"}],
            },
        )

    data = response.json()["data"]
    row = audit_mod.query_llm_audit(minutes=10, scope="all")[0]
    assert response.status_code == 200
    assert data["mode"] == "failed"
    assert data["outline_failure_stage"] == "candidate_create"
    assert data["outline_failure_code"] == "candidate_create_failed"
    assert "database path" not in response.text
    assert "SQL" not in response.text
    assert row["postprocess_stage"] == "candidate_create"
    assert row["postprocess_code"] == "candidate_create_failed"


def test_outline_chat_extracts_patch_from_explanation_with_trailing_braces(monkeypatch):
    from app.services import ai_service

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, *, role=None, **_kwargs):
            assert role == ROLE_PLANNING
            patch_payload = json.dumps({
                "patchMode": "merge",
                "upsertNodes": [{"id": "a1", "summary": "第一弧形成完整的起因、升级与后果。"}],
                "removeNodeIds": [],
            }, ensure_ascii=False)
            return f"以下是可应用补丁：\n{patch_payload}\n说明：保留 {{已有正文章节}}。"

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "测试"})
        project_id = created.json()["data"]["id"]
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": _outline("旧主线")})
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化大纲第一弧"}]},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["candidate"]["status"] == "proposed"
    nodes = {node["id"]: node for node in data["candidate"]["candidateOutline"]["nodes"]}
    assert nodes["a1"]["summary"] == "第一弧形成完整的起因、升级与后果。"


def test_outline_chat_merges_a_small_model_patch_into_the_complete_outline(monkeypatch):
    from app.services import ai_service

    original = _outline("旧主线")
    original["nodes"].extend([
        {"id": "a2", "type": "arc", "parentId": "v1", "title": "第二弧", "summary": "保留弧", "order": 2},
        {"id": "c2", "type": "chapter", "parentId": "v1", "arcId": "a2", "title": "第二章", "order": 2},
    ])

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, messages, *, role=None, **_kwargs):
            prompt = "\n".join(str(message.get("content") or "") for message in messages)
            assert "a1" in prompt
            assert "a2" in prompt
            assert role == ROLE_PLANNING
            return json.dumps({
                "patchMode": "merge",
                "mainSummary": "优化后的主线",
                "upsertNodes": [
                    {"id": "a1", "summary": "第一弧从压迫升级到不可逆失去，并把后果留给下一弧。"},
                ],
                "removeNodeIds": [],
            }, ensure_ascii=False)

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "测试"})
        project_id = created.json()["data"]["id"]
        client.patch(
            f"/api/projects/{project_id}",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"outline": original},
        )
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化大纲，把第一弧写得更完整"}]},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["candidate"]["status"] == "proposed"
    candidate_nodes = {node["id"]: node for node in data["candidate"]["candidateOutline"]["nodes"]}
    assert candidate_nodes["a1"]["summary"] == "第一弧从压迫升级到不可逆失去，并把后果留给下一弧。"
    assert candidate_nodes["a2"]["summary"] == "保留弧"
    assert candidate_nodes["c1"]["arcId"] == "a1"
    assert candidate_nodes["c2"]["arcId"] == "a2"


def test_outline_chat_creates_candidate_for_server_validated_arc_replacement(monkeypatch):
    from app.services import ai_service

    original = _outline("旧主线")
    original["nodes"] = original["nodes"][:2] + [
        {"id": f"c{order}", "type": "chapter", "parentId": "v1", "arcId": "a1", "title": f"第{order}章", "order": order}
        for order in range(1, 7)
    ]

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, messages, *, role=None, **_kwargs):
            prompt = "\n".join(str(message.get("content") or "") for message in messages)
            assert "arcReplacements" in prompt
            assert '"chapterCount":6' in prompt
            return json.dumps({
                "arcReplacements": [{
                    "volumeId": "v1",
                    "arcs": [
                        {"title": "日常一", "summary": "完整小故事一。", "startChapter": 1, "endChapter": 2},
                        {"title": "日常二", "summary": "完整小故事二。", "startChapter": 3, "endChapter": 4},
                        {"title": "觉醒", "summary": "梧渡开始觉醒。", "startChapter": 5, "endChapter": 6},
                    ],
                }],
            }, ensure_ascii=False)

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "测试"})
        project_id = created.json()["data"]["id"]
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": original})
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "调整第一卷，每两章为一弧"}]},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["candidate"]["status"] == "proposed"
    assert len([node for node in data["candidate"]["candidateOutline"]["nodes"] if node["type"] == "arc"]) == 3


def test_outline_chat_accepts_wrapped_patch_and_filters_model_metadata(monkeypatch):
    from app.services import ai_service

    class Provider:
        provider = "mock"
        model = "mock-outline"

        async def chat(self, _messages, *, role=None, **_kwargs):
            assert role == ROLE_PLANNING
            return "```json\n" + json.dumps({
                "outline": {
                    "patchMode": "merge",
                    "updates": [{"nodeId": "a1", "summary": "新的弧线总结", "reason": "用户要求"}],
                },
            }, ensure_ascii=False) + "\n```"

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner:user-a")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: Provider())
    _request_windows.clear()

    with TestClient(app) as client:
        created = client.post("/api/projects/", headers={"X-NovelCraft-Invite": "owner"}, json={"name": "测试"})
        project_id = created.json()["data"]["id"]
        client.patch(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "owner"}, json={"outline": _outline("旧主线")})
        response = client.post(
            "/api/ai/chat",
            headers={"X-NovelCraft-Invite": "owner"},
            json={"projectId": project_id, "messages": [{"role": "user", "content": "请优化大纲，把第一弧写得更完整"}]},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["candidate"]["status"] == "proposed"
    candidate_nodes = {node["id"]: node for node in data["candidate"]["candidateOutline"]["nodes"]}
    assert candidate_nodes["a1"]["summary"] == "新的弧线总结"


def test_outline_payload_extractor_skips_unrelated_json_before_patch():
    from app.services.ai_service import _extract_outline_payload

    reply = (
        '处理状态：{"status":"ok","note":"分析完成"}\n'
        '可应用补丁：{"patchMode":"merge","upsertNodes":[{"id":"a1","summary":"新弧"}],"removeNodeIds":[]}'
    )

    payload = _extract_outline_payload(reply)

    assert payload["patchMode"] == "merge"
    assert payload["upsertNodes"][0]["id"] == "a1"
