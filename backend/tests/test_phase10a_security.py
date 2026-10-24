"""Phase 10-A deterministic beta-security coverage. No provider calls."""

from fastapi.testclient import TestClient
from fastapi import UploadFile
from io import BytesIO

from app.core.config import settings
from app.db import get_conn, init_db
from app.llm.audit import clear_llm_audit_context, set_llm_audit_context
from app.llm.provider import LLMResponseError
from app.services.project_budget_service import consume_project_budget
from app.api.imports import _store_uploaded_file
from main import app


def _project_payload(name: str) -> dict:
    return {"name": name, "genre": "xuanhuan", "targetWordCount": 10000, "stylePreset": "epic"}


def test_project_is_visible_only_to_its_owner(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "alpha:user-a,beta:user-b", raising=False)
    with TestClient(app) as client:
        created = client.post("/api/projects/", json=_project_payload("A 的项目"), headers={"X-NovelCraft-Invite": "alpha"})
        assert created.status_code == 200
        project_id = created.json()["data"]["id"]

        denied = client.get(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "beta"})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "无权访问此项目。"

        allowed = client.get(f"/api/projects/{project_id}", headers={"X-NovelCraft-Invite": "alpha"})
        assert allowed.status_code == 200


def test_production_rejects_server_path_import(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "alpha:user-a", raising=False)
    source = tmp_path / "novel.md"
    source.write_text("# 章节", encoding="utf-8")
    with TestClient(app) as client:
        response = client.post(
            "/api/import/suggestion-docx",
            data={"filePath": str(source)},
            headers={"X-NovelCraft-Invite": "alpha"},
        )
    assert response.status_code in {400, 403}
    assert response.json()["detail"] == "生产环境仅支持上传文件。"


def test_upload_rejects_disallowed_extension_and_size(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "upload_max_bytes", 3)
    bad = UploadFile(filename="unsafe.doc", file=BytesIO(b"x"))
    try:
        _store_uploaded_file(bad)
        assert False, "expected extension rejection"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    too_large = UploadFile(filename="text.txt", file=BytesIO(b"1234"))
    try:
        _store_uploaded_file(too_large)
        assert False, "expected size rejection"
    except Exception as exc:
        assert getattr(exc, "detail", None) == "上传文件超过大小上限。"


def test_project_daily_budget_blocks_before_provider_call(monkeypatch):
    init_db()
    project_id = "budget-project"
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO projects (id, owner_id, name, created_at, updated_at, style_config) VALUES (?, ?, ?, ?, ?, ?)", (project_id, "local-dev", "预算", "2026-01-01", "2026-01-01", "{}"))
    monkeypatch.setattr(settings, "project_daily_llm_call_limit", 0)
    set_llm_audit_context(project_id=project_id, merge=False)
    try:
        consume_project_budget(1)
        assert False, "expected project budget rejection"
    except LLMResponseError as exc:
        assert str(exc) == "今日项目 AI 使用额度已达到上限。"
    finally:
        clear_llm_audit_context()


def test_production_rate_limit_returns_429(monkeypatch):
    from main import _request_windows
    _request_windows.clear()
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "alpha:user-a")
    monkeypatch.setattr(settings, "rate_limit", 1)
    with TestClient(app) as client:
        first = client.get("/api/projects/", headers={"X-NovelCraft-Invite": "alpha"})
        second = client.get("/api/projects/", headers={"X-NovelCraft-Invite": "alpha"})
    assert first.status_code == 200
    assert second.status_code == 429


def test_production_rate_limit_allows_configured_quota_then_blocks(monkeypatch):
    """Project-list reads must consume the normal production request quota."""
    import main

    main._request_windows.clear()
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "alpha:user-a")
    monkeypatch.setattr(settings, "rate_limit", 2)
    monkeypatch.setattr(main.time, "monotonic", lambda: 100.0)
    with TestClient(app) as client:
        responses = [
            client.get("/api/projects/", headers={"X-NovelCraft-Invite": "alpha"})
            for _ in range(3)
        ]
    assert [response.status_code for response in responses] == [200, 200, 429]


def test_production_rate_limit_recovers_after_window(monkeypatch):
    """Expired timestamps must not continue blocking a request bucket."""
    import main

    main._request_windows.clear()
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "alpha:user-a")
    monkeypatch.setattr(settings, "rate_limit", 1)
    # Seed an expired timestamp so the middleware's real clock can prune it.
    main._request_windows["testclient"] = [main.time.monotonic() - 61.0]
    with TestClient(app) as client:
        after_window = client.get("/api/projects/", headers={"X-NovelCraft-Invite": "alpha"})
    assert after_window.status_code == 200


def test_restart_preserves_project_chapter_and_story_state():
    from app.dao import chapters, projects
    from app.services import story_state_service
    init_db()
    project_id = "recovery-project"
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO projects (id, owner_id, name, created_at, updated_at, style_config) VALUES (?, ?, ?, ?, ?, ?)", (project_id, "local-dev", "恢复", "2026-01-01", "2026-01-01", "{}"))
        conn.execute("INSERT OR REPLACE INTO chapters (id, project_id, title, chapter_order, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", ("recovery-chapter", project_id, "第一章", 1, "正文", "2026-01-01", "2026-01-01"))
    story_state_service.ensure_story_state(project_id)
    init_db()  # Deterministic stand-in for restarting the backend process.
    assert projects.get_project(project_id)["name"] == "恢复"
    assert chapters.get_chapter("recovery-chapter", project_id)["content"] == "正文"
    assert story_state_service.get_story_state(project_id)["project_id"] == project_id


def test_unauthenticated_settings_are_rejected_without_provider_call(monkeypatch):
    from app.llm import provider
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "")
    called = {"count": 0}
    monkeypatch.setattr(provider.llm, "chat", lambda *args, **kwargs: called.__setitem__("count", called["count"] + 1))
    with TestClient(app) as client:
        assert client.get("/api/settings/llm-configs").status_code == 401
        assert client.get("/api/settings/role-profiles").status_code == 401
        assert client.get("/api/settings/llm-health").status_code == 401
    assert called["count"] == 0


def test_user_cannot_call_review_or_story_state_on_other_project(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "a:user-a,b:user-b")
    with TestClient(app) as client:
        created = client.post("/api/projects/", json=_project_payload("review scope"), headers={"X-NovelCraft-Invite": "a"})
        pid = created.json()["data"]["id"]
        review = client.post("/api/review/comprehensive", json={"projectId": pid, "text": "正文"}, headers={"X-NovelCraft-Invite": "b"})
        state = client.get(f"/api/projects/{pid}/story-state", headers={"X-NovelCraft-Invite": "b"})
    assert review.status_code == 403
    assert state.status_code == 403


def test_user_cannot_call_ai_style_or_memory_on_other_project(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "a:user-a,b:user-b")
    with TestClient(app) as client:
        created = client.post("/api/projects/", json=_project_payload("all scopes"), headers={"X-NovelCraft-Invite": "a"})
        pid = created.json()["data"]["id"]
        headers = {"X-NovelCraft-Invite": "b"}
        ai = client.post("/api/ai/generate-character", json={"projectId": pid}, headers=headers)
        style = client.get(f"/api/projects/{pid}/style-profile", headers=headers)
        memory = client.get(f"/api/projects/{pid}/writing-experiences", headers=headers)
    assert ai.status_code == 403
    assert style.status_code == 403
    assert memory.status_code == 403
