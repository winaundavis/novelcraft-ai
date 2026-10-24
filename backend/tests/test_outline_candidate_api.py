from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import settings
from main import _request_windows, app


OWNER = {"X-NovelCraft-Invite": "owner-token"}
OTHER = {"X-NovelCraft-Invite": "other-token"}


def _outline(summary: str = "旧主线") -> dict:
    return {
        "mainSummary": summary,
        "nodes": [
            {"id": "v1", "type": "volume", "parentId": None, "title": "第一卷", "order": 1},
            {"id": "a1", "type": "arc", "parentId": "v1", "title": "第一弧", "order": 1},
            {"id": "c1", "type": "chapter", "parentId": "v1", "arcId": "a1", "title": "第一章", "order": 1},
        ],
    }


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/api/projects/",
        headers=OWNER,
        json={"name": "候选测试", "genre": "xuanhuan", "targetWordCount": 10000, "stylePreset": "epic"},
    )
    assert response.status_code == 200
    project_id = response.json()["data"]["id"]
    saved = client.patch(f"/api/projects/{project_id}", headers=OWNER, json={"outline": _outline()})
    assert saved.status_code == 200
    return project_id


def _create_candidate(client: TestClient, project_id: str, summary: str = "新主线") -> dict:
    response = client.post(
        f"/api/projects/{project_id}/outline-candidates",
        headers=OWNER,
        json={"candidateOutline": _outline(summary), "instruction": "优化主线", "runId": "run-test"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner-token:user-a,other-token:user-b")
    _request_windows.clear()


def test_non_owner_cannot_apply_outline_candidate(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        candidate = _create_candidate(client, project_id)

        denied = client.post(f"/api/outline-candidates/{candidate['id']}/apply", headers=OTHER)

        assert denied.status_code == 403
        current = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["outline"]
        assert current["mainSummary"] == "旧主线"


def test_owner_can_apply_and_revert_candidate(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        candidate = _create_candidate(client, project_id)

        applied = client.post(f"/api/outline-candidates/{candidate['id']}/apply", headers=OWNER)
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["status"] == "applied"
        assert client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["outline"]["mainSummary"] == "新主线"

        reverted = client.post(f"/api/outline-candidates/{candidate['id']}/revert", headers=OWNER)
        assert reverted.status_code == 200, reverted.text
        assert reverted.json()["data"]["status"] == "reverted"
        assert client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["outline"]["mainSummary"] == "旧主线"


def test_apply_returns_409_when_outline_changed_after_candidate_creation(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        candidate = _create_candidate(client, project_id)
        assert client.patch(f"/api/projects/{project_id}", headers=OWNER, json={"outline": _outline("用户刚刚修改")}).status_code == 200

        response = client.post(f"/api/outline-candidates/{candidate['id']}/apply", headers=OWNER)

        assert response.status_code == 409
        current = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["outline"]
        assert current["mainSummary"] == "用户刚刚修改"


def test_owner_can_list_and_reject_own_candidates(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        candidate = _create_candidate(client, project_id)

        listed = client.get(f"/api/projects/{project_id}/outline-candidates", headers=OWNER)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["data"]] == [candidate["id"]]

        rejected = client.post(f"/api/outline-candidates/{candidate['id']}/reject", headers=OWNER)
        assert rejected.status_code == 200
        assert rejected.json()["data"]["status"] == "rejected"


def test_saved_chapter_outline_removal_requires_explicit_confirmation_and_preserves_content(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project(client)
        created_chapter = client.post(
            f"/api/projects/{project_id}/chapters/",
            headers=OWNER,
            json={"title": "第一章", "volumeId": "v1", "order": 1},
        )
        assert created_chapter.status_code == 200, created_chapter.text
        chapter_id = created_chapter.json()["data"]["id"]
        assert client.put(
            f"/api/projects/{project_id}/chapters/{chapter_id}",
            headers=OWNER,
            json={"content": "这段正文必须保留。"},
        ).status_code == 200

        current = _outline()
        current["nodes"][-1]["id"] = chapter_id
        assert client.patch(
            f"/api/projects/{project_id}",
            headers=OWNER,
            json={"outline": current},
        ).status_code == 200
        proposed = _outline("删除章节后的主线")
        proposed["nodes"] = [node for node in proposed["nodes"] if node["type"] != "chapter"]

        response = client.post(
            f"/api/projects/{project_id}/outline-candidates",
            headers=OWNER,
            json={"candidateOutline": proposed, "instruction": "删除第一章", "runId": "run-delete"},
        )
        assert response.status_code == 200, response.text
        candidate = response.json()["data"]
        assert candidate["requiresConfirmation"] is True
        assert candidate["protectedRemovals"] == [{"id": chapter_id, "title": "第一章"}]

        blocked = client.post(f"/api/outline-candidates/{candidate['id']}/apply", headers=OWNER)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "saved_chapter_removal_confirmation_required"

        applied = client.post(
            f"/api/outline-candidates/{candidate['id']}/apply?confirmSavedChapterRemoval=true",
            headers=OWNER,
        )
        assert applied.status_code == 200, applied.text
        outline = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["outline"]
        assert chapter_id not in {node["id"] for node in outline["nodes"]}
        chapter = client.get(f"/api/projects/{project_id}/chapters/{chapter_id}", headers=OWNER)
        assert chapter.status_code == 404
        recycled = client.get(f"/api/projects/{project_id}/chapter-trash", headers=OWNER).json()["data"]["records"]
        assert len(recycled) == 1
        preserved = client.get(f"/api/projects/{project_id}/chapter-trash/{recycled[0]['id']}", headers=OWNER)
        assert preserved.json()["data"]["content"] == "这段正文必须保留。"
