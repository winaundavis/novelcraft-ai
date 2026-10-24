"""Phase 10-C ownership closure tests.  All provider calls are mocked."""

from fastapi.testclient import TestClient

from app.core.config import settings
from main import app


def _project_payload(name: str) -> dict:
    return {"name": name, "genre": "xuanhuan", "targetWordCount": 10000, "stylePreset": "epic"}


def _create_owned_project(client: TestClient) -> str:
    from app.dao import users

    ids = {}
    for username in ("user-a", "user-b"):
        account = users.find_user(username)
        if account is None:
            user_id = users.create_user(username, "strong-pass-1")
        else:
            user_id = account["id"]
        ids[username] = user_id
    settings.invite_tokens = f"a:{ids['user-a']},b:{ids['user-b']}"
    response = client.post("/api/projects/", json=_project_payload("A 的项目"), headers={"X-NovelCraft-Invite": "a"})
    assert response.status_code == 200
    return response.json()["data"]["id"]


def test_wrong_owner_cannot_read_or_mutate_project_scoped_routes(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "a:user-a,b:user-b")
    owner = {"X-NovelCraft-Invite": "a"}
    other = {"X-NovelCraft-Invite": "b"}
    with TestClient(app) as client:
        project_id = _create_owned_project(client)
        chapter = client.post(f"/api/projects/{project_id}/chapters/", json={"title": "第一章", "volumeId": "v1", "order": 1}, headers=owner)
        assert chapter.status_code == 200
        chapter_id = chapter.json()["data"]["id"]
        requests = [
            ("get", f"/api/projects/{project_id}/chapters/", None),
            ("post", f"/api/projects/{project_id}/chapters/", {"title": "越权", "volumeId": "v1", "order": 2}),
            ("put", f"/api/projects/{project_id}/chapters/{chapter_id}", {"content": "越权正文"}),
            ("delete", f"/api/projects/{project_id}/chapters/{chapter_id}", None),
            ("get", f"/api/projects/{project_id}/story-state", None),
            ("patch", f"/api/projects/{project_id}/story-state", {"mainConflict": "越权"}),
            ("get", f"/api/projects/{project_id}/writing-experiences", None),
            ("post", f"/api/projects/{project_id}/writing-experiences", {"experience_type": "recurring_problem", "observation": "越权"}),
            ("get", f"/api/projects/{project_id}/character-voices", None),
            ("get", f"/api/projects/{project_id}/foreshadows/", None),
            ("get", f"/api/projects/{project_id}/readiness", None),
            ("get", f"/api/projects/{project_id}/fulltext-stats", None),
            ("post", f"/api/projects/{project_id}/chapters/replay", None),
        ]
        for method, path, payload in requests:
            if method in {"get", "delete"}:
                response = getattr(client, method)(path, headers=other)
            else:
                response = getattr(client, method)(path, json=payload, headers=other)
            assert response.status_code == 403, f"{method.upper()} {path}: {response.text}"
        assert client.get(f"/api/projects/{project_id}/chapters/{chapter_id}", headers=owner).status_code == 200


def test_unauthorized_fulltext_never_calls_provider(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "a:user-a,b:user-b")
    from app.services import fulltext_service

    calls = {"count": 0}

    async def ask(*_args, **_kwargs):
        calls["count"] += 1
        return {"answer": "unexpected"}

    monkeypatch.setattr(fulltext_service, "ask_fulltext", ask)
    with TestClient(app) as client:
        project_id = _create_owned_project(client)
        denied = client.post(f"/api/projects/{project_id}/fulltext-ask", json={"question": "内容是什么？"}, headers={"X-NovelCraft-Invite": "b"})
        missing = client.post(f"/api/projects/{project_id}/fulltext-ask", json={"question": "内容是什么？"})
    assert denied.status_code == 403
    assert missing.status_code == 401
    assert calls["count"] == 0


def test_wrong_owner_cannot_delete_project_or_replay_projection(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "a:user-a,b:user-b")
    from app.services import projection_runner_service
    from main import _request_windows
    _request_windows.clear()

    calls = {"count": 0}
    monkeypatch.setattr(projection_runner_service, "replay_project", lambda *_args: calls.__setitem__("count", calls["count"] + 1))
    with TestClient(app) as client:
        project_id = _create_owned_project(client)
        headers = {"X-NovelCraft-Invite": "b"}
        assert client.post(f"/api/projects/{project_id}/chapters/replay", headers=headers).status_code == 403
        assert client.delete(f"/api/projects/{project_id}", headers=headers).status_code == 403
        assert client.get(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "a"}).status_code == 200
    assert calls["count"] == 0


def test_wrong_owner_cannot_list_recoverable_job_without_latest_job(monkeypatch):
    """Recoverable jobs remain private even when no latest/running job is present."""
    from app.dao import novel_jobs as novel_jobs_dao, users

    monkeypatch.setattr(settings, "debug", False)
    owner_id = users.create_user("recoverable-owner", "strong-pass-1")
    other_id = users.create_user("recoverable-other", "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"owner:{owner_id},other:{other_id}")
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/",
            json=_project_payload("recoverable ownership"),
            headers={"X-NovelCraft-Invite": "owner"},
        )
        assert project.status_code == 200
        project_id = project.json()["data"]["id"]
        novel_jobs_dao.save_job({
            "id": "recoverable-owner-only",
            "project_id": project_id,
            "mode": "all",
            "status": "interrupted",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
        })

        denied = client.get(
            f"/api/ai/project/{project_id}/recoverable",
            headers={"X-NovelCraft-Invite": "other"},
        )
        assert denied.status_code == 403
