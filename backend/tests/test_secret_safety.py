"""Credential checks use synthetic data and temporary paths only.

Exercise the real release scanner, settings response masking, and production
logging. No local dotenv, user database, or external report directory is read.
"""

from copy import deepcopy
import json
import logging
import zipfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app.api.settings import _mask_api_key, _public_llm_config
from app.core.config import settings
from app.core.production_logging import build_request_event, log_request
from app.dao import llm_configs
from main import app
from scripts.release_audit import audit_target


@pytest.fixture(autouse=True)
def _no_live_model_transport(live_llm_call_spy):
    yield
    assert live_llm_call_spy() == 0


def test_public_config_masks_all_credentials_without_mutating_storage(tmp_path):
    primary_key = "sk-" + "A" * 30
    role_key = "sk-" + "B" * 28
    stored = {
        "id": "synthetic-config",
        "provider": "deepseek",
        "apiKey": primary_key,
        "model": "synthetic-model",
        "roleOverrides": {
            "writing": {"api_key": role_key, "model": "synthetic-writing"},
            "check": {"model": "synthetic-review"},
        },
    }
    before = deepcopy(stored)

    public = _public_llm_config(stored)

    assert stored == before
    assert public["apiKey"] == "sk-A" + "*" * 25 + "AAAA"
    assert public["roleOverrides"]["writing"]["api_key"] == "sk-B" + "*" * 23 + "BBBB"
    assert public["roleOverrides"]["writing"]["model"] == "synthetic-writing"
    assert public["roleOverrides"]["check"] == {"model": "synthetic-review"}
    report_file = tmp_path / "config-report.json"
    report_file.write_text(json.dumps(stored), encoding="utf-8")
    assert audit_target(tmp_path)["passed"] is False
    report_file.write_text(json.dumps(public), encoding="utf-8")
    assert audit_target(tmp_path)["passed"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", ""), ("x", "*"), ("shortkey", "********")],
    ids=["empty", "one-character", "eight-characters"],
)
def test_short_credentials_never_expose_the_original(value, expected):
    assert _mask_api_key(value) == expected


def test_settings_create_and_list_never_return_primary_or_role_credentials(monkeypatch):
    primary_key = "sk-" + "C" * 30
    role_key = "sk-" + "D" * 28
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "admin_user_ids", "local-dev")
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode("ascii"))
    config_id = None

    try:
        with TestClient(app) as client:
            created = client.post("/api/settings/llm-configs", json={
                "provider": "deepseek",
                "apiKey": primary_key,
                "model": "synthetic-model",
                "isActive": False,
                "roleOverrides": {"writing": {"api_key": role_key, "model": "synthetic-writing"}},
            })
            assert created.status_code == 200
            config_id = created.json()["data"]["id"]
            listed = client.get("/api/settings/llm-configs")
            assert listed.status_code == 200
            rows = [created.json()["data"], next(row for row in listed.json()["data"] if row["id"] == config_id)]
            for row in rows:
                assert row["apiKey"] == "sk-C" + "*" * 25 + "CCCC"
                assert row["roleOverrides"]["writing"]["api_key"] == "sk-D" + "*" * 23 + "DDDD"
            for response in (created, listed):
                assert primary_key not in response.text
                assert role_key not in response.text
            # Masking must not discard the actual credentials needed by the provider.
            stored = llm_configs.get_config(config_id)
            assert stored["apiKey"] == primary_key
            assert stored["roleOverrides"]["writing"]["api_key"] == role_key
    finally:
        if config_id is not None:
            llm_configs.delete_config(config_id)


@pytest.mark.parametrize("filename", ["summary.json", "notes.md", "diagnostic.txt"])
def test_release_audit_rejects_embedded_credentials_without_echoing_them(tmp_path, filename):
    key = "sk-" + "E" * 30
    (tmp_path / filename).write_text(json.dumps({"provider_key": key}), encoding="utf-8")

    report = audit_target(tmp_path)

    assert report["status"] == "FAIL"
    assert report["summary"]["files_scanned"] == 1
    assert report["violations"] == [{"path": filename, "rule": "embedded_secret_pattern", "action": "blocked"}]
    assert key not in json.dumps(report)


@pytest.mark.parametrize(
    ("relative", "rule"),
    [
        ("backups/role-overrides.json", "database_backup"),
        ("diagnostics/request.log", "runtime_log_or_audit"),
        ("backend/.env.production", "forbidden_environment_file"),
        ("private/app.sqlite3", "database_or_journal"),
    ],
    ids=["backup", "log", "dotenv", "database"],
)
def test_release_audit_blocks_private_artifacts_even_without_key_patterns(tmp_path, relative, rule):
    artifact = tmp_path / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("synthetic private artifact\n", encoding="utf-8")

    report = audit_target(tmp_path)

    assert report["passed"] is False
    assert rule in {item["rule"] for item in report["violations"]}
    assert artifact.read_text(encoding="utf-8") == "synthetic private artifact\n"


def test_env_sample_accepts_placeholder_but_rejects_credential_assignment(tmp_path):
    sample = tmp_path / ".env.production.example"
    sample.write_text("ARTS_API_KEY=REPLACE_WITH_PRIVATE_SECRET\n", encoding="utf-8")
    assert audit_target(tmp_path)["passed"] is True
    # No known vendor prefix: sample assignments must be rejected independently
    # of the high-confidence key patterns.
    key = "synthetic-" + "F" * 30
    sample.write_text("ARTS_API_KEY=" + key + "\n", encoding="utf-8")

    report = audit_target(tmp_path)

    assert report["passed"] is False
    assert report["violations"] == [{
        "path": ".env.production.example", "rule": "embedded_secret_assignment", "action": "blocked",
    }]
    assert key not in json.dumps(report)


def test_zip_release_audit_rejects_embedded_credentials_without_extracting(tmp_path):
    key = "sk-" + "G" * 30
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("novelcraft-ai/reports/summary.json", json.dumps({"api_key": key}))

    report = audit_target(archive)

    assert report["passed"] is False
    assert report["violations"] == [{
        "path": "novelcraft-ai/reports/summary.json", "rule": "embedded_secret_pattern", "action": "blocked",
    }]
    assert key not in json.dumps(report)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["candidate.zip"]


def test_production_log_omits_credential_and_body_but_keeps_operational_metadata(tmp_path, caplog):
    key = "sk-" + "H" * 30
    body = "synthetic private manuscript body"
    event = build_request_event(
        request_id="synthetic-request", user_id="synthetic-user", project_id="synthetic-project",
        operation="POST /api/ai/chat", status=502, latency_ms=12.34,
        detail="Authorization: Bearer " + key + "; body=" + body,
    )

    with caplog.at_level(logging.INFO, logger="novelcraft.secret-safety"):
        log_request(logging.getLogger("novelcraft.secret-safety"), event)

    logged = [record.getMessage() for record in caplog.records if record.name == "novelcraft.secret-safety"]
    assert len(logged) == 1
    assert json.loads(logged[0]) == {
        "request_id": "synthetic-request", "user_id": "synthetic-user", "project_id": "synthetic-project",
        "operation": "POST /api/ai/chat", "status": 502, "latency_ms": 12.34,
        "usage": None, "cost": None, "detail": "redacted",
    }
    assert key not in logged[0]
    assert body not in logged[0]
    (tmp_path / "request-report.json").write_text(logged[0], encoding="utf-8")
    assert audit_target(tmp_path)["passed"] is True
