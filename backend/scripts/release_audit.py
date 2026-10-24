"""Audit a NovelCraft release directory or ZIP without exposing its contents.

The audit is deliberately stricter than ``package_source.py``.  It is a
read-only gate for a *candidate* release and reports only relative paths and
rule codes.  It never prints file contents, credential values, or exception
text that could contain them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


RULE_VERSION = "release-audit-v2.1"
MAX_CONTENT_BYTES = 2 * 1024 * 1024

FORBIDDEN_DIRS = frozenset(
    {
        ".git",
        ".edge-cdp",
        ".edge-profile",
        ".npm-cache",
        ".pytest_cache",
        "__pycache__",
        "build",
        "backup",
        "backups",
        "coverage",
        "data",
        "dist",
        "htmlcov",
        "log",
        "logs",
        "node_modules",
        "playwright-report",
        "test-artifacts",
        "test_artifacts",
        "test-results",
        "test_results",
        "session_data",
        "session-data",
        "sessions",
        "user_data",
        "user-data",
        "uploads",
        ".venv",
        "venv",
    }
)

BROWSER_STATE_NAMES = frozenset(
    {
        "bookmarks",
        "cookies",
        "extension cookies",
        "history",
        "local state",
        "login data",
        "preferences",
        "secure preferences",
        "session",
        "web data",
    }
)

ALLOWED_ENV_SUFFIXES = (".example", ".sample", ".template")
DB_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
)
BACKUP_SUFFIXES = (".bak", ".backup", ".dump", ".old", ".bak.gz", ".backup.gz", ".dump.gz", ".old.gz")
LOG_SUFFIXES = (".log", ".out", ".err", ".trace", ".jsonl")
PRIVATE_KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer")

_HIGH_CONFIDENCE_SECRET_PATTERNS = (
    # Keep these patterns literal enough that regex definitions in source
    # code are not mistaken for leaked credentials.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z][0-9A-Za-z_-]{19,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z]{16,}\b"),
)
_SECRET_ASSIGNMENT = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]*(?:API_KEY|SECRET|TOKEN(?:S)?|PASSWORD))\s*=\s*(.*?)\s*$",
    re.IGNORECASE,
)
# Configuration names that contain TOKEN but are numeric limits rather than
# credentials.  They must remain scannable without making the release gate
# reject the shipped example configuration.
_NON_SECRET_ASSIGNMENTS = frozenset(
    {
        "DEFAULT_MAX_TOKENS",
        "PROJECT_DAILY_TOKEN_LIMIT",
        "PROJECT_DAILY_LLM_CALL_LIMIT",
        "RATE_LIMIT",
        "UPLOAD_MAX_BYTES",
        "FULLTEXT_MAX_CHARS",
    }
)
_PLACEHOLDER_MARKERS = (
    "REPLACE_WITH",
    "EXAMPLE.INVALID",
    "YOUR_",
    "YOUR-",
    "CHANGE_ME",
    "CHANGEME",
    "PLACEHOLDER",
    "INSERT_",
    "<REPLACE",
)


def _is_allowed_env_name(name: str) -> bool:
    lower = name.casefold()
    return lower == ".env.example" or lower.startswith(".env.") and lower.endswith(ALLOWED_ENV_SUFFIXES)


def _is_env_name(name: str) -> bool:
    lower = name.casefold()
    return lower == ".env" or lower.startswith(".env.")


def _is_placeholder(value: str) -> bool:
    cleaned = value.strip().strip("'\"").strip()
    if not cleaned or cleaned in {"...", "null", "none", "${...}"}:
        return True
    upper = cleaned.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS) or (
        cleaned.startswith("${") and cleaned.endswith("}")
    ) or (cleaned.startswith("<") and cleaned.endswith(">"))


def _path_parts(name: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PurePosixPath(name).parts if part not in {"", "."})


def _classify_path(name: str) -> str | None:
    """Return a stable rule code for a forbidden path, if any."""
    lower = name.casefold()
    parts = _path_parts(name)
    if any(part in {".edge-profile", ".edge-cdp"} for part in parts):
        return "browser_profile"
    if any(part in {"backup", "backups"} for part in parts):
        return "database_backup"
    if any(part in {"log", "logs"} for part in parts):
        return "runtime_log_or_audit"
    if any(part in FORBIDDEN_DIRS for part in parts):
        return "forbidden_runtime_directory"
    basename = parts[-1] if parts else ""
    if basename in BROWSER_STATE_NAMES:
        return "browser_state"
    if _is_env_name(basename) and not _is_allowed_env_name(basename):
        return "forbidden_environment_file"
    if lower.endswith(DB_SUFFIXES):
        return "database_or_journal"
    if lower.endswith(BACKUP_SUFFIXES) or ".pre-restore-" in lower:
        return "database_backup"
    if lower.endswith(PRIVATE_KEY_SUFFIXES) or basename.startswith(("id_rsa", "id_ed25519")):
        return "private_key_or_certificate"
    if lower.endswith(LOG_SUFFIXES):
        return "runtime_log_or_audit"
    if lower.endswith(".zip"):
        return "nested_archive"
    return None


def _read_text_bytes(data: bytes) -> str | None:
    if len(data) > MAX_CONTENT_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _content_rules(text: str, *, env_sample: bool) -> list[str]:
    rules: list[str] = []
    if any(pattern.search(text) for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS):
        rules.append("embedded_secret_pattern")
    if env_sample:
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = _SECRET_ASSIGNMENT.match(line)
            if not match:
                continue
            if match.group(1).upper() in _NON_SECRET_ASSIGNMENTS:
                continue
            value = match.group(2).split("#", 1)[0].strip()
            if value and not _is_placeholder(value):
                rules.append("embedded_secret_assignment")
                break
    return rules


class _Audit:
    def __init__(self, target: Path, kind: str):
        self.target = target
        self.kind = kind
        self.files_scanned = 0
        self.content_scanned = 0
        self.violations: list[dict[str, str]] = []

    def add(self, path: str, rule: str) -> None:
        item = {"path": path.replace("\\", "/"), "rule": rule, "action": "blocked"}
        if item not in self.violations:
            self.violations.append(item)

    def report(self) -> dict:
        return {
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "target": str(self.target),
            "kind": self.kind,
            "rule_version": RULE_VERSION,
            "status": "PASS" if not self.violations else "FAIL",
            "passed": not self.violations,
            "summary": {
                "files_scanned": self.files_scanned,
                "content_scanned": self.content_scanned,
                "violation_count": len(self.violations),
            },
            "violations": self.violations,
        }


def _scan_content(audit: _Audit, name: str, data: bytes) -> None:
    if data.startswith(b"SQLite format 3\x00"):
        audit.add(name, "database_content_header")
        return
    text = _read_text_bytes(data)
    if text is None:
        return
    audit.content_scanned += 1
    for rule in _content_rules(text, env_sample=_is_allowed_env_name(PurePosixPath(name).name)):
        audit.add(name, rule)


def _audit_directory(target: Path) -> dict:
    audit = _Audit(target, "directory")
    root = target.resolve()

    def on_error(_error: OSError) -> None:
        audit.add(".", "directory_unreadable")

    for current, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=on_error):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        for directory in list(directories):
            full = current_path / directory
            relative = (relative_dir / directory).as_posix()
            if full.is_symlink():
                audit.add(relative, "symbolic_link")
                directories.remove(directory)
                continue
            if _classify_path(relative):
                audit.add(relative, _classify_path(relative) or "forbidden_runtime_directory")
                directories.remove(directory)
        for filename in files:
            full = current_path / filename
            relative = (relative_dir / filename).as_posix()
            audit.files_scanned += 1
            if full.is_symlink():
                audit.add(relative, "symbolic_link")
                continue
            rule = _classify_path(relative)
            if rule:
                audit.add(relative, rule)
                continue
            try:
                with full.open("rb") as handle:
                    _scan_content(audit, relative, handle.read(MAX_CONTENT_BYTES + 1))
            except OSError:
                audit.add(relative, "file_unreadable")
    return audit.report()


def _normalise_zip_name(name: str) -> tuple[str | None, str | None]:
    if not name or "\x00" in name:
        return None, "invalid_archive_path"
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None, "invalid_archive_path"
    pure = PurePosixPath(name)
    if ".." in pure.parts:
        return None, "invalid_archive_path"
    return pure.as_posix(), None


def _audit_zip(target: Path) -> dict:
    audit = _Audit(target, "zip")
    try:
        with zipfile.ZipFile(target) as archive:
            seen: set[str] = set()
            for info in archive.infolist():
                name, invalid_rule = _normalise_zip_name(info.filename)
                if invalid_rule or name is None:
                    audit.add(info.filename or "<empty>", invalid_rule or "invalid_archive_path")
                    continue
                if name in seen:
                    audit.add(name, "duplicate_archive_path")
                    continue
                seen.add(name)
                is_directory = info.is_dir() or info.filename.endswith("/")
                if is_directory:
                    rule = _classify_path(name)
                    if rule:
                        audit.add(name, rule)
                    continue
                audit.files_scanned += 1
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    audit.add(name, "symbolic_link")
                    continue
                rule = _classify_path(name)
                if rule:
                    audit.add(name, rule)
                    continue
                try:
                    with archive.open(info, "r") as handle:
                        _scan_content(audit, name, handle.read(MAX_CONTENT_BYTES + 1))
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    audit.add(name, "file_unreadable")
    except (OSError, zipfile.BadZipFile):
        audit.add("<archive>", "invalid_archive")
    return audit.report()


def audit_target(target: str | Path) -> dict:
    path = Path(target)
    if not path.exists():
        audit = _Audit(path, "unknown")
        audit.add(path.name or ".", "target_missing")
        return audit.report()
    if path.is_dir():
        return _audit_directory(path)
    if path.is_file() and zipfile.is_zipfile(path):
        return _audit_zip(path)
    audit = _Audit(path, "unknown")
    audit.add(path.name, "unsupported_target")
    return audit.report()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a NovelCraft release directory or ZIP")
    parser.add_argument("target", type=Path)
    parser.add_argument("--pretty", action="store_true", help="pretty-print the JSON report")
    parser.add_argument("--json-out", type=Path, help="also write the redacted report to this file")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = audit_target(args.target)
    rendered = json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
