"""T2 端到端：Copilot Accept 后的正文 / 项目汇总 / FTS 派生索引一致性。

与 `test_phase_d_copilot_candidates.py` 的桩库单测不同，这里使用 pytest 的
临时隔离数据库与应用真实 schema（含 `chapter_fts` FTS5 表），因此能验证：
- Accept 前正文与项目总字数不变；
- Accept 后正文更新、项目总字数等于活动章节合计；
- FTS 索引确实被同步（命中来源为 `fts`，而不是 LIKE 兜底）；
- 被替换掉的旧词在检索中消失；
- 重复接受按现有契约拒绝（一次性）。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.dao import memory_fts as memory_fts_dao
from app.services import ai_service
from main import app

ORIGINAL = "雨停后，她把青铜信封放回抽屉。"
REPLACEMENT = "雨停后，她把青铜钥匙交给门外的人。"
NEW_KEYWORD = "青铜钥匙"
OLD_KEYWORD = "青铜信封"


def _project(client: TestClient) -> str:
    response = client.post("/api/projects/", json={"name": "accept-sync-e2e"})
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _chapter(client: TestClient, project_id: str, content: str) -> str:
    created = client.post(
        f"/api/projects/{project_id}/chapters/",
        json={"title": "第一章", "volumeId": "", "order": 1},
    )
    assert created.status_code == 200, created.text
    chapter_id = created.json()["data"]["id"]
    saved = client.put(
        f"/api/projects/{project_id}/chapters/{chapter_id}",
        json={"content": content},
    )
    assert saved.status_code == 200, saved.text
    return chapter_id


def _accept(client: TestClient, project_id: str, chapter_id: str, selection: str, replacement: str) -> dict:
    with patch.object(
        ai_service, "chat_reply",
        new=AsyncMock(return_value={"reply": replacement, "context": {}}),
    ):
        created = client.post("/api/copilot/candidates", json={
            "projectId": project_id,
            "chapterId": chapter_id,
            "action": "rewrite",
            "selectionText": selection,
            "currentText": selection,
        })
    assert created.status_code == 200, created.text
    candidate_id = created.json()["data"]["id"]
    accepted = client.post(f"/api/copilot/candidates/{candidate_id}/accept")
    assert accepted.status_code == 200, accepted.text
    return {"candidate_id": candidate_id, "payload": accepted.json()["data"]}


def test_accept_keeps_prose_project_total_and_fts_index_consistent():
    with TestClient(app) as client:
        project_id = _project(client)
        chapter_id = _chapter(client, project_id, ORIGINAL)

        before_project = client.get(f"/api/projects/{project_id}").json()["data"]
        before_chapters = client.get(f"/api/projects/{project_id}/chapters").json()["data"]
        assert before_project["currentWordCount"] == sum(item["wordCount"] for item in before_chapters)

        result = _accept(client, project_id, chapter_id, ORIGINAL, REPLACEMENT)

        # 返回体带 applied_content（最近修复的契约）且等于最终正文
        assert result["payload"]["applied_content"] == REPLACEMENT
        assert result["payload"]["status"] == "applied"

        final_chapter = client.get(f"/api/projects/{project_id}/chapters/{chapter_id}").json()["data"]
        assert final_chapter["content"] == REPLACEMENT

        after_project = client.get(f"/api/projects/{project_id}").json()["data"]
        after_chapters = client.get(f"/api/projects/{project_id}/chapters").json()["data"]
        assert after_project["currentWordCount"] == sum(item["wordCount"] for item in after_chapters)
        assert after_project["currentWordCount"] == final_chapter["wordCount"]

        # 新词可检索，且命中来源必须是 FTS 索引（LIKE 兜底不算通过）
        hits = memory_fts_dao.search_relevant(project_id, [NEW_KEYWORD])
        assert [hit["chapter_id"] for hit in hits] == [chapter_id]
        assert hits[0]["source"] == "fts"

        # 被替换掉的旧词在检索中消失
        assert memory_fts_dao.search_relevant(project_id, [OLD_KEYWORD]) == []

        # 一次性：重复接受按现有状态契约拒绝，且不产生第二次覆盖
        repeat = client.post(f"/api/copilot/candidates/{result['candidate_id']}/accept")
        assert repeat.status_code >= 400
        assert client.get(f"/api/projects/{project_id}/chapters/{chapter_id}").json()["data"]["content"] == REPLACEMENT


def test_accept_is_rejected_after_manual_edit_changes_the_source():
    """原文已被手工修改时，Accept 必须拒绝覆盖（保留最新正文保护）。"""
    with TestClient(app) as client:
        project_id = _project(client)
        chapter_id = _chapter(client, project_id, ORIGINAL)

        with patch.object(
            ai_service, "chat_reply",
            new=AsyncMock(return_value={"reply": REPLACEMENT, "context": {}}),
        ):
            created = client.post("/api/copilot/candidates", json={
                "projectId": project_id, "chapterId": chapter_id,
                "action": "rewrite", "selectionText": ORIGINAL, "currentText": ORIGINAL,
            })
        candidate_id = created.json()["data"]["id"]

        # 手工把正文改成别的内容（候选基于旧版本）
        edited = "雨停后，她改写了完全不同的段落。"
        client.put(f"/api/projects/{project_id}/chapters/{chapter_id}", json={"content": edited})

        rejected = client.post(f"/api/copilot/candidates/{candidate_id}/accept")
        assert rejected.status_code >= 400
        assert client.get(f"/api/projects/{project_id}/chapters/{chapter_id}").json()["data"]["content"] == edited


def test_accept_requires_project_ownership(monkeypatch):
    """非 owner 不得接受候选（owner 保护不被 Accept 绕过）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner-token:user-owner,other-token:user-other")
    monkeypatch.setattr(settings, "admin_user_ids", "")

    with TestClient(app) as client:
        owner = {"X-NovelCraft-Invite": "owner-token"}
        other = {"X-NovelCraft-Invite": "other-token"}
        created = client.post("/api/projects/", json={"name": "owned"}, headers=owner)
        project_id = created.json()["data"]["id"]
        chapter = client.post(
            f"/api/projects/{project_id}/chapters/",
            json={"title": "第一章", "volumeId": "", "order": 1}, headers=owner,
        ).json()["data"]
        chapter_id = chapter["id"]
        client.put(
            f"/api/projects/{project_id}/chapters/{chapter_id}",
            json={"content": ORIGINAL}, headers=owner,
        )

        with patch.object(
            ai_service, "chat_reply",
            new=AsyncMock(return_value={"reply": REPLACEMENT, "context": {}}),
        ):
            candidate = client.post("/api/copilot/candidates", json={
                "projectId": project_id, "chapterId": chapter_id,
                "action": "rewrite", "selectionText": ORIGINAL, "currentText": ORIGINAL,
            }, headers=owner)
        candidate_id = candidate.json()["data"]["id"]

        denied = client.post(f"/api/copilot/candidates/{candidate_id}/accept", headers=other)
        assert denied.status_code == 403
        assert client.get(
            f"/api/projects/{project_id}/chapters/{chapter_id}", headers=owner
        ).json()["data"]["content"] == ORIGINAL
