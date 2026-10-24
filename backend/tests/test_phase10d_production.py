"""Phase 10-D production-hardening tests. No live provider calls."""

from io import BytesIO

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers


def test_data_dir_and_database_override_are_resolved(tmp_path):
    from app.core.config import resolve_cors_origins, resolve_data_dir, resolve_database_path

    data_dir = resolve_data_dir(str(tmp_path / "private-data"))
    assert data_dir == (tmp_path / "private-data").resolve()
    assert resolve_database_path(data_dir, "") == data_dir / "novelcraft.db"
    assert resolve_database_path(data_dir, str(tmp_path / "custom" / "app.db")) == (tmp_path / "custom" / "app.db").resolve()
    assert resolve_cors_origins("https://app.example.com, https://admin.example.com", debug=False) == ["https://app.example.com", "https://admin.example.com"]
    assert resolve_cors_origins("", debug=False) == []
    assert resolve_cors_origins("", debug=True) == ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"]


def test_production_config_gate_rejects_placeholder_and_accepts_private_values():
    from cryptography.fernet import Fernet
    from app.core.config import Settings, production_config_errors

    private_key = Fernet.generate_key().decode("ascii")
    valid = Settings(
        _env_file=None,
        environment="beta",
        debug=False,
        cors_allow_origins="https://app.example.com",
        allowed_hosts="app.example.com",
        auth_cookie_secure=True,
        invite_tokens="",
        admin_user_ids="admin-user-id",
        app_encryption_key=private_key,
    )
    assert production_config_errors(valid) == []

    invalid = valid.model_copy(update={
        "admin_user_ids": "REPLACE_WITH_REGISTERED_USER_ID",
        "invite_tokens": "REPLACE_WITH_PRIVATE_INVITE:legacy-beta-user",
        "app_encryption_key": "REPLACE_WITH_FERNET_KEY",
    })
    assert production_config_errors(invalid) == [
        "admin_placeholder",
        "encryption_key_not_configured",
        "invite_placeholder",
    ]


def test_public_readiness_hides_production_config_error_codes(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.core.config import settings
    from main import app

    db_path = tmp_path / "private" / "novelcraft.db"
    db_path.parent.mkdir(parents=True)
    db_path.touch()
    monkeypatch.setattr(settings, "environment", "beta")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "cors_allow_origins", "")
    monkeypatch.setattr(settings, "allowed_hosts", "")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "admin_user_ids", "REPLACE_WITH_REGISTERED_USER_ID")
    monkeypatch.setattr(settings, "app_encryption_key", "REPLACE_WITH_FERNET_KEY")
    monkeypatch.setattr(settings, "data_dir", str(db_path.parent))
    monkeypatch.setattr(settings, "novelcraft_db_path", str(db_path))

    with TestClient(app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "version": "1.0.0"}
    assert "admin_placeholder" not in response.text
    assert "FERNET" not in response.text


def test_public_readiness_blocks_until_configured_admin_exists(monkeypatch, tmp_path):
    """A syntactically valid admin ID must not make beta ready before initialization."""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from app import db as app_db
    from app.core.config import settings
    from main import app

    db_path = tmp_path / "private" / "novelcraft.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(app_db, "DB_PATH", db_path)
    monkeypatch.setattr(settings, "environment", "beta")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "cors_allow_origins", "https://app.example.com")
    monkeypatch.setattr(settings, "allowed_hosts", "app.example.com")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "admin_user_ids", "missing-admin-for-readiness")
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(settings, "data_dir", str(db_path.parent))
    monkeypatch.setattr(settings, "novelcraft_db_path", str(db_path))

    with TestClient(app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "version": "1.0.0"}


def test_public_readiness_accepts_existing_is_admin_bootstrap(monkeypatch):
    """An existing database admin is a valid initialization path."""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from app import db as app_db
    from app.core.config import settings
    from app.dao import users
    from main import app

    admin_id = users.create_user("readiness-bootstrap-admin", "strong-pass-1", is_admin=True)
    monkeypatch.setattr(settings, "environment", "beta")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "cors_allow_origins", "https://app.example.com")
    monkeypatch.setattr(settings, "allowed_hosts", "app.example.com")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "admin_user_ids", "stale-admin-id")
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(settings, "data_dir", str(app_db.DB_PATH.parent))
    monkeypatch.setattr(settings, "novelcraft_db_path", str(app_db.DB_PATH))

    with TestClient(app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "version": "1.0.0"}


def test_database_connection_creates_cold_production_data_directory(monkeypatch, tmp_path):
    from app import db

    database = tmp_path / "cold" / "private-data" / "novelcraft.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    with db.get_conn() as conn:
        conn.execute("SELECT 1")
    assert database.parent.is_dir()


def test_upload_rejects_invalid_docx_signature_and_binary_text(monkeypatch, tmp_path):
    from app.api.imports import _store_uploaded_file
    from app.core.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    with pytest.raises(Exception) as invalid_docx:
        _store_uploaded_file(UploadFile(filename="fake.docx", file=BytesIO(b"not-a-zip")))
    assert getattr(invalid_docx.value, "status_code", None) == 400

    with pytest.raises(Exception) as binary_text:
        _store_uploaded_file(UploadFile(filename="binary.txt", file=BytesIO(b"hello\x00world")))
    assert getattr(binary_text.value, "status_code", None) == 400

    with pytest.raises(Exception) as mismatched_mime:
        _store_uploaded_file(
            UploadFile(filename="notes.txt", file=BytesIO(b"plain text"), headers=Headers({"content-type": "image/png"}))
        )
    assert getattr(mismatched_mime.value, "status_code", None) == 400


def test_production_log_event_is_metadata_only_and_redacted():
    from app.core.production_logging import build_request_event

    event = build_request_event(
        request_id="req-1",
        user_id="user-1",
        project_id="project-1",
        operation="POST /api/projects/project-1/fulltext-ask",
        status=403,
        latency_ms=7.25,
        detail="Authorization: Bearer super-secret-key; body=entire novel text",
    )
    assert event["request_id"] == "req-1"
    assert event["user_id"] == "user-1"
    assert event["project_id"] == "project-1"
    assert event["operation"] == "POST /api/projects/project-1/fulltext-ask"
    assert event["status"] == 403
    assert event["latency_ms"] == 7.25
    assert event["usage"] is None
    assert event["cost"] is None
    assert "super-secret-key" not in event["detail"]
    assert "entire novel text" not in event["detail"]


def test_production_example_has_required_placeholders():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".env.production.example").read_text(encoding="utf-8")
    required = {
        "DEBUG=false",
        "CORS_ALLOW_ORIGINS=",
        "DATA_DIR=",
        "NOVELCRAFT_DB_PATH=",
        "UPLOAD_DIR=",
        "UPLOAD_MAX_BYTES=",
        "RATE_LIMIT=",
        "PROJECT_DAILY_LLM_CALL_LIMIT=",
        "PROJECT_DAILY_TOKEN_LIMIT=",
        "USER_DAILY_CHAPTER_LIMIT=",
        "INVITE_TOKENS=",
        "ADMIN_USER_IDS=",
        "VITE_API_BASE_URL=/api",
        "LOG_LEVEL=",
    }
    assert all(item in text for item in required)


def test_frontend_api_base_defaults_to_same_origin_and_allows_override():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "services" / "api.ts").read_text(encoding="utf-8")
    assert "import.meta.env.VITE_API_BASE_URL || '/api'" in source


def test_production_request_logging_omits_request_body(monkeypatch, caplog):
    import logging
    from fastapi.testclient import TestClient
    from app.core.config import settings
    from app.dao import users
    from main import _request_windows, app

    _request_windows.clear()
    caplog.set_level(logging.INFO, logger="novelcraft.request")
    monkeypatch.setattr(settings, "debug", False)
    user_id = users.create_user("prod-log-user", "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"alpha:{user_id}")
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/",
            json={"name": "不应写入日志的正文", "genre": "xuanhuan", "targetWordCount": 1000, "stylePreset": "epic"},
            headers={"X-NovelCraft-Invite": "alpha"},
        )
    assert response.status_code == 200
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert '"request_id"' in messages
    assert f'"user_id":"{user_id}"' in messages
    assert '"operation":"POST /api/projects/"' in messages
    assert "不应写入日志的正文" not in messages


def test_backup_restore_refuses_to_overwrite_without_explicit_replace(tmp_path):
    from scripts.backup_sqlite import backup_database, restore_database

    database = tmp_path / "data" / "novelcraft.db"
    archive = tmp_path / "backups" / "novelcraft.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"original")
    backup_database(database, archive)
    database.write_bytes(b"newer")

    with pytest.raises(FileExistsError):
        restore_database(archive, database)
    preserved = restore_database(archive, database, replace=True)
    assert preserved is not None and preserved.read_bytes() == b"newer"
    assert database.read_bytes() == b"original"


def test_production_smoke_health_login_save_and_reopen(monkeypatch):
    from fastapi.testclient import TestClient
    from app.core.config import settings
    from app.dao import users
    from app.db import init_db
    from main import _request_windows, app

    _request_windows.clear()
    monkeypatch.setattr(settings, "debug", False)
    user_id = users.create_user("prod-smoke-user", "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"alpha:{user_id}")
    headers = {"X-NovelCraft-Invite": "alpha"}
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok", "version": "1.0.0"}
        created = client.post("/api/projects/", json={"name": "生产 smoke", "genre": "xuanhuan", "targetWordCount": 1000, "stylePreset": "epic"}, headers=headers)
        assert created.status_code == 200
        project_id = created.json()["data"]["id"]
        chapter = client.post(f"/api/projects/{project_id}/chapters/", json={"title": "第一章", "volumeId": "v1", "order": 1}, headers=headers)
        chapter_id = chapter.json()["data"]["id"]
        assert client.put(f"/api/projects/{project_id}/chapters/{chapter_id}", json={"content": "可恢复正文"}, headers=headers).status_code == 200
        init_db()
        reopened = client.get(f"/api/projects/{project_id}/chapters/{chapter_id}", headers=headers)
    assert reopened.status_code == 200
    assert reopened.json()["data"]["content"] == "可恢复正文"


def test_production_hides_internal_provider_error_detail(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import ai
    from app.core.config import settings
    from app.dao import users
    from app.llm.provider import LLMResponseError
    from main import _request_windows, app

    async def fail_generate(*_args, **_kwargs):
        raise LLMResponseError("provider credential-should-not-leak")

    _request_windows.clear()
    monkeypatch.setattr(settings, "debug", False)
    user_id = users.create_user("prod-error-user", "strong-pass-1")
    monkeypatch.setattr(settings, "invite_tokens", f"alpha:{user_id}")
    monkeypatch.setattr(ai, "generate_outline", fail_generate)
    headers = {"X-NovelCraft-Invite": "alpha"}
    with TestClient(app) as client:
        project = client.post("/api/projects/", json={"name": "错误脱敏", "genre": "xuanhuan", "targetWordCount": 1000, "stylePreset": "epic"}, headers=headers)
        response = client.post("/api/ai/generate-outline", json={"projectId": project.json()["data"]["id"], "premise": "test"}, headers=headers)
    assert response.status_code == 502
    assert response.json()["detail"] == "服务暂时不可用，请稍后重试。"
