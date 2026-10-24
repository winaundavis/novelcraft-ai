"""Release audit tests use synthetic files only; no real credentials or data."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts.package_source import build_archive
from scripts.release_audit import audit_target, main


def _write(root: Path, relative: str, content: bytes | str = "synthetic\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_clean_directory_and_placeholder_env_pass(tmp_path):
    root = tmp_path / "release"
    root.mkdir()
    _write(root, "backend/app/main.py", "print('ok')\n")
    _write(root, ".env.production.example", "DEBUG=false\nARTS_API_KEY=REPLACE_WITH_PRIVATE_SECRET\n")
    _write(root, "test-fixtures/sample.md", "fixture\n")

    report = audit_target(root)

    assert report["passed"] is True
    assert report["summary"]["violation_count"] == 0
    assert "REPLACE_WITH_PRIVATE_SECRET" not in json.dumps(report)


def test_directory_rejects_sensitive_paths_and_simulated_key(tmp_path):
    root = tmp_path / "release"
    root.mkdir()
    _write(root, "data/app.sqlite3", b"synthetic")
    _write(root, "backups/app.db.20261017.bak", b"synthetic")
    _write(root, "backend/.env", "DEEPSEEK_API_KEY=sk-simulated-secret-value\n")
    _write(root, "config.txt", "provider token: sk-" + "A" * 24 + "\n")
    _write(root, "backend/private.pem", "-----BEGIN " + "PRIVATE KEY-----\n")
    _write(root, "logs/server.log", "request log\n")
    _write(root, ".edge-profile/Default/Cookies", b"synthetic")

    report = audit_target(root)

    assert report["passed"] is False
    rules = {item["rule"] for item in report["violations"]}
    assert {
        "forbidden_runtime_directory",
        "forbidden_environment_file",
        "private_key_or_certificate",
        "runtime_log_or_audit",
        "browser_profile",
    } <= rules
    rendered = json.dumps(report, ensure_ascii=False)
    assert "sk-simulated" not in rendered


def test_zip_is_audited_without_extracting_and_rejects_traversal(tmp_path):
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("novelcraft-ai/backend/app/main.py", "ok\n")
        zipped.writestr("novelcraft-ai/.env.production.example", "ARTS_API_KEY=sk-simulated-secret-value\n")
        zipped.writestr("novelcraft-ai/../../outside.txt", "bad\n")

    report = audit_target(archive)

    assert report["passed"] is False
    assert {item["rule"] for item in report["violations"]} >= {
        "embedded_secret_assignment",
        "invalid_archive_path",
    }
    assert "sk-simulated" not in json.dumps(report, ensure_ascii=False)


def test_clean_zip_passes(tmp_path):
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("novelcraft-ai/backend/app/main.py", "ok\n")
        zipped.writestr("novelcraft-ai/.env.production.example", "ARTS_API_KEY=REPLACE_WITH_PRIVATE_SECRET\n")

    report = audit_target(archive)

    assert report["passed"] is True
    assert report["summary"]["violation_count"] == 0


def test_rollback_journals_are_rejected_outside_runtime_directories(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    for suffix in (".db-journal", ".sqlite-journal", ".sqlite3-journal"):
        _write(root, "store/app" + suffix)
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for source in (root / "store").iterdir():
            zipped.write(source, "store/" + source.name)
    for target in (root, archive):
        report = audit_target(target)
        assert report["passed"] is False
        assert len(report["violations"]) == 3
        assert all(item["rule"] == "database_or_journal" for item in report["violations"])


def test_package_source_uses_relative_parts_for_exclusions(tmp_path):
    # The checkout itself may live under a directory called ``data``.  That
    # ancestor must not make every source file disappear from the archive.
    root = tmp_path / "data" / "novelcraft-ai"
    _write(root, "backend/app/main.py", "ok\n")
    archive = build_archive(root, tmp_path / "candidate.zip")

    with zipfile.ZipFile(archive) as zipped:
        assert "novelcraft-ai/backend/app/main.py" in zipped.namelist()


def test_cli_returns_nonzero_for_failed_audit(tmp_path, capsys):
    root = tmp_path / "release"
    root.mkdir()
    _write(root, "backend/.env", "DEEPSEEK_API_KEY=sk-simulated-secret-value\n")

    assert main([str(root)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"
    assert report["summary"]["violation_count"] >= 1


def test_cli_json_out_writes_the_same_redacted_report(tmp_path, capsys):
    root = tmp_path / "release"
    root.mkdir()
    _write(root, "backend/app/main.py", "ok\n")
    output = tmp_path / "audit" / "report.json"

    assert main([str(root), "--json-out", str(output)]) == 0
    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert printed == saved
def test_renamed_large_sqlite_file_is_rejected_by_content(tmp_path):
    from scripts.release_audit import audit_target
    import zipfile

    root = tmp_path / "candidate"
    root.mkdir()
    renamed = root / "innocent.bin"
    renamed.write_bytes(b"SQLite format 3\x00" + b"\x00" * (2 * 1024 * 1024))
    report = audit_target(root)
    assert not report["passed"]
    assert any(v["rule"] == "database_content_header" for v in report["violations"])
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.write(renamed, "innocent.bin")
    report = audit_target(archive)
    assert not report["passed"]
    assert any(v["rule"] == "database_content_header" for v in report["violations"])

