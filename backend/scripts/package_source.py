"""Create and verify a portable NovelCraft source archive without API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


EXCLUDED_PARTS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".venv", "venv", "htmlcov", ".edge-cdp", ".edge-profile", ".npm-cache",
    "dist", "build", "coverage", "test-results", "playwright-report",
    "test_artifacts", "data", "uploads", "log", "logs", "backup", "backups",
    "session_data", "session-data", "sessions", "user_data", "user-data",
    "release-artifacts",
}
# 归档不得包含：数据库（含 WAL/SHM 旁文件）、备份、编译产物、日志。
EXCLUDED_SUFFIXES = {
    ".db", ".db-wal", ".db-shm", ".db-journal",
    ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
    ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal",
    ".bak", ".backup", ".dump", ".old",
    ".pyc", ".log", ".out", ".err", ".trace", ".jsonl",
    ".tsbuildinfo",
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".zip",
}
# 允许分发的示例配置（不含真实凭据）；其余 .env / .env.* 一律排除。
_ALLOWED_ENV_NAMES = {".env.example", ".env.sample", ".env.template"}
_ALLOWED_ENV_SUFFIXES = (".example", ".sample", ".template")
REQUIRED_PATHS = (
    "novelcraft-ai/backend/app/services/projection_runner_service.py",
    "novelcraft-ai/backend/app/services/experiment_001_service.py",
    "novelcraft-ai/backend/tests/conftest.py",
    "novelcraft-ai/backend/tests/test_phase7a_experiment_preparation.py",
    "novelcraft-ai/test-fixtures/sample-novel/01-sample-novel.md",
    "novelcraft-ai/docs/PUBLIC_BETA_DEPLOYMENT.md",
    "novelcraft-ai/docs/PUBLIC_BETA_GO_LIVE_CHECKLIST.md",
    "novelcraft-ai/docs/user-guide.md",
)

# The source checkout contains internal audit history and agent hand-offs.  A
# public candidate only needs the user/deployment documents below; shipping the
# rest would disclose account/project identifiers and internal operations even
# when no credential scanner matches them.
_PUBLIC_DOC_NAMES = {
    "PUBLIC_BETA_DEPLOYMENT.md",
    "PUBLIC_BETA_GO_LIVE_CHECKLIST.md",
    "PUBLIC_BETA_FINAL_REPORT.md",
    "user-guide.md",
}


def _source_files(source_root: Path, excluded: Path) -> list[Path]:
    """Reject links before reading; a ZIP cannot reveal dereferenced origins."""
    files: list[Path] = []

    def verify(path: Path) -> None:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("source contains a link or reparse point")
        if not path.resolve().is_relative_to(source_root):
            raise ValueError("source file resolves outside the source root")

    for current, directories, filenames in os.walk(source_root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            if not _include(path, excluded_path=excluded, relative_to=source_root):
                directories.remove(name)
            else:
                verify(path)
        for name in filenames:
            path = current_path / name
            if _include(path, excluded_path=excluded, relative_to=source_root):
                verify(path)
                files.append(path)
    return sorted(files)


def _is_secret_env_file(name: str) -> bool:
    """`.env` / `.env.*` 视为敏感，但保留 `*.example` / `*.sample` / `*.template` 样例。"""
    lower = name.lower()
    if lower in _ALLOWED_ENV_NAMES or lower.endswith(_ALLOWED_ENV_SUFFIXES):
        return False
    return lower == ".env" or lower.startswith(".env.")


def _include(
    path: Path,
    *,
    excluded_path: Path | None = None,
    relative_to: Path | None = None,
) -> bool:
    if excluded_path is not None and path.resolve() == excluded_path:
        # 归档自身（输出可能写在源码树内）绝不能被打进包里。
        return False
    # Filter only the path inside the source tree.  Looking at ``path.parts``
    # directly would also inspect absolute parent directories, so a checkout
    # such as ``C:\\data\\novelcraft-ai`` would accidentally exclude every
    # file because ``data`` is a private runtime directory name.
    parts = path.relative_to(relative_to).parts if relative_to is not None else path.parts
    if any(part in EXCLUDED_PARTS for part in parts):
        return False
    if parts and parts[0] in {"memory", "skills", "tools", "scripts"}:
        # Local workflows, tunnels and old mutation harnesses are not deployment
        # dependencies. Supported maintenance tools live in backend/scripts/.
        return False
    if path.name.startswith("_qa_"):
        return False  # Historical ad-hoc harness; keep it only in the checkout.
    if len(parts) == 1 and path.name in {"AGENTS.md", "CHANGELOG.md", "release-candidate.json"}:
        return False  # Internal history stays in the checkout, outside public delivery.
    if "docs" in parts and not path.is_dir():
        # Keep only the small, externally useful documentation surface.  This
        # is intentionally a filename allowlist rather than a history-based
        # pattern: old reports frequently contain real project identifiers.
        if path.name not in _PUBLIC_DOC_NAMES:
            return False
    lower_name = path.name.lower()
    if path.suffix.lower() in EXCLUDED_SUFFIXES or lower_name.endswith((".bak.gz", ".backup.gz", ".dump.gz", ".old.gz")):
        return False
    if _is_secret_env_file(path.name):
        return False
    return True


def build_archive(source_root: Path, destination: Path) -> Path:
    """Write POSIX ZIP names, rooted exactly as an extracted source checkout."""
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ValueError("source directory does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    excluded = destination.resolve()
    files: list[dict[str, str]] = []
    selected = _source_files(source_root, excluded)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in selected:
            if path.is_file() and _include(path, excluded_path=excluded, relative_to=source_root):
                relative_path = path.relative_to(source_root).as_posix()
                if relative_path == "release-build.json":
                    continue  # Always generate provenance for these exact bytes.
                data = path.read_bytes()
                archive.writestr((Path(source_root.name) / relative_path).as_posix(), data)
                files.append({"path": relative_path, "sha256": hashlib.sha256(data).hexdigest()})
        try:
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source_root, text=True, stderr=subprocess.DEVNULL,
            ).strip()
            dirty = bool(subprocess.check_output(
                ["git", "status", "--porcelain", "--", "."], cwd=source_root, stderr=subprocess.DEVNULL,
            ).strip())
        except (OSError, subprocess.CalledProcessError):
            head, dirty = None, None
        provenance = {
            "schemaVersion": 1,
            "builtAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sourceHead": head,
            "sourceWorkingTreeDirty": dirty,
            "sourceIdentity": "per-file SHA-256; sourceHead alone does not identify uncommitted changes",
            "historicalMetadata": "checkout release-candidate.json is historical and excluded from public delivery",
            "files": files,
        }
        archive.writestr(
            (Path(source_root.name) / "release-build.json").as_posix(),
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        )
    return destination


def validate_archive(archive_path: Path, extract_root: Path | None = None) -> dict:
    archive_path = archive_path.resolve()
    owned_temp = extract_root is None
    work = extract_root or Path(tempfile.mkdtemp(prefix="novelcraft-zip-check-"))
    if work.exists():
        work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        invalid = [name for name in names if "\\" in name or name.startswith("/") or ".." in Path(name).parts]
        if invalid:
            raise ValueError("archive has invalid path entries: " + ", ".join(invalid[:3]))
        archive.extractall(work)
    missing = [item for item in REQUIRED_PATHS if not (work / item).is_file()]
    report = {"archive": str(archive_path), "entries": len(names), "valid": not missing, "missing": missing, "extracted_to": str(work)}
    if owned_temp:
        shutil.rmtree(work, ignore_errors=True)
    if missing:
        raise ValueError("archive is missing required paths: " + ", ".join(missing))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    archive = build_archive(Path(args.source), Path(args.output))
    print(validate_archive(archive))
