r"""T6：交付源码包排除边界（合成树，不读任何真实凭据/真实数据）。

覆盖：
- secret dotenv（`.env` / `.env.production` / `.env.local`）被排除；
- 保留 `.env.example` / `.env.production.example` 等样例；
- SQLite 主库与 WAL/SHM 旁文件、`.bak` 备份被排除；
- 浏览器 profile 与测试产物目录被排除；
- 合法测试 fixture 保留；
- 归档自身（输出写在源码树内时）不会被打进包；
- 归一化 POSIX 路径（无 `\`、无绝对路径、无 `..`）。

本用例只构造合成文件，不读取真实 API key / 用户数据。
"""
from pathlib import Path
import hashlib
import json
import zipfile
import pytest

from scripts.package_source import build_archive


def _make_tree(root: Path) -> None:
    files = [
        "backend/.env",
        "backend/.env.production",
        "backend/.env.local",
        "backend/.env.example",
        "backend/.env.production.example",
        "backend/app/main.py",
        "data/novelcraft.db",
        "data/novelcraft.db-wal",
        "store/app.sqlite3",
        "store/app.sqlite3-shm",
        "store/app.db-journal",
        "store/app.sqlite-journal",
        "store/app.sqlite3-journal",
        "store/app.db.bak",
        "store/old.db.backup",
        "test_artifacts/report.json",
        ".edge-profile/Cookies",
        ".edge-cdp/port",
        "frontend/tests/fixtures/sample-novel.md",
        "frontend/src/components/memory/ProjectionStatusPanel.tsx",
        "backend/tests/conftest.py",
        "memory/PROJECT.md",
        "skills/internal-workflow.md",
        "docs/internal-audit.md",
        "docs/PUBLIC_BETA_DEPLOYMENT.md",
        "docs/user-guide.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "release-candidate.json",
        "tools/cloudflared.exe",
        "scripts/cleanup_test_data.py",
        "backend/_qa_synthetic_memory_test.py",
    ]
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # 合成内容：绝不包含真实凭据。
        target.write_text(f"synthetic:{relative}\n", encoding="utf-8")


def test_archive_excludes_secrets_databases_and_test_artifacts(tmp_path):
    root = tmp_path / "novelcraft-ai"
    root.mkdir()
    _make_tree(root)

    # 输出故意写在源码树内部：归档自身必须被排除。
    archive = build_archive(root, root / "dist-out" / "novelcraft-source.zip")

    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())

    # 1) 敏感 dotenv 被排除
    assert "novelcraft-ai/backend/.env" not in names
    assert "novelcraft-ai/backend/.env.production" not in names
    assert "novelcraft-ai/backend/.env.local" not in names

    # 2) 样例配置保留
    assert "novelcraft-ai/backend/.env.example" in names
    assert "novelcraft-ai/backend/.env.production.example" in names

    # 3) SQLite / WAL / 备份被排除
    for excluded in (
        "novelcraft-ai/data/novelcraft.db",
        "novelcraft-ai/data/novelcraft.db-wal",
        "novelcraft-ai/store/app.sqlite3",
        "novelcraft-ai/store/app.sqlite3-shm",
        "novelcraft-ai/store/app.db-journal",
        "novelcraft-ai/store/app.sqlite-journal",
        "novelcraft-ai/store/app.sqlite3-journal",
        "novelcraft-ai/store/app.db.bak",
        "novelcraft-ai/store/old.db.backup",
    ):
        assert excluded not in names, f"{excluded} 不应被打进源码包"

    # 4) 浏览器 profile 与测试产物被排除
    assert not any(name.startswith("novelcraft-ai/test_artifacts/") for name in names)
    assert not any(name.startswith("novelcraft-ai/.edge-profile/") for name in names)
    assert not any(name.startswith("novelcraft-ai/.edge-cdp/") for name in names)

    # 5) 合法测试 fixture 与源码保留
    assert "novelcraft-ai/frontend/tests/fixtures/sample-novel.md" in names
    assert "novelcraft-ai/backend/tests/conftest.py" in names
    assert "novelcraft-ai/backend/app/main.py" in names
    assert "novelcraft-ai/frontend/src/components/memory/ProjectionStatusPanel.tsx" in names
    assert "novelcraft-ai/memory/PROJECT.md" not in names
    assert "novelcraft-ai/skills/internal-workflow.md" not in names
    assert "novelcraft-ai/docs/internal-audit.md" not in names
    assert "novelcraft-ai/docs/PUBLIC_BETA_DEPLOYMENT.md" in names
    assert "novelcraft-ai/docs/user-guide.md" in names
    assert "novelcraft-ai/AGENTS.md" not in names
    assert "novelcraft-ai/CHANGELOG.md" not in names
    assert "novelcraft-ai/release-candidate.json" not in names
    assert "novelcraft-ai/tools/cloudflared.exe" not in names
    assert "novelcraft-ai/scripts/cleanup_test_data.py" not in names
    assert "novelcraft-ai/backend/_qa_synthetic_memory_test.py" not in names

    # 6) 归档自身不被打包
    assert not any(name.endswith("novelcraft-source.zip") for name in names)

    # 7) POSIX 路径规范
    for name in names:
        assert "\\" not in name, f"非 POSIX 归档路径: {name}"
        assert not name.startswith("/"), f"归档路径不得为绝对路径: {name}"
        assert ".." not in Path(name).parts, f"归档路径不得包含 ..: {name}"


def test_archive_does_not_delete_the_original_tree(tmp_path):
    """打包只是读取：原始文件（含被排除项）必须原样保留。"""
    root = tmp_path / "novelcraft-ai"
    root.mkdir()
    _make_tree(root)
    secret = root / "backend" / ".env"
    before = secret.read_text(encoding="utf-8")

    build_archive(root, tmp_path / "out.zip")

    assert secret.exists()
    assert secret.read_text(encoding="utf-8") == before
    assert (root / "data" / "novelcraft.db").exists()
    assert (root / "test_artifacts" / "report.json").exists()


def test_build_provenance_hashes_the_exact_archived_bytes(tmp_path):
    root = tmp_path / "novelcraft-ai"
    root.mkdir()
    _make_tree(root)
    # A stale copied manifest must not be accepted as the current build identity.
    (root / "release-build.json").write_text('{"stale":true}', encoding="utf-8")
    archive = build_archive(root, tmp_path / "candidate.zip")
    with zipfile.ZipFile(archive) as zipped:
        provenance_name = "novelcraft-ai/release-build.json"
        assert zipped.namelist().count(provenance_name) == 1
        meta = json.loads(zipped.read(provenance_name))
        assert meta["schemaVersion"] == 1
        assert meta["builtAtUtc"].endswith("Z")
        assert "sourceHead" in meta and "sourceWorkingTreeDirty" in meta
        hashes = {item["path"]: item["sha256"] for item in meta["files"]}
        assert set(hashes) == {
            name.removeprefix("novelcraft-ai/")
            for name in zipped.namelist() if name != provenance_name
        }
        for path, digest in hashes.items():
            assert hashlib.sha256(zipped.read("novelcraft-ai/" + path)).hexdigest() == digest
        assert "backend/.env" not in hashes


def test_archive_rejects_a_link_to_files_outside_the_source_tree(tmp_path, monkeypatch):
    root = tmp_path / "novelcraft-ai"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("synthetic private content", encoding="utf-8")
    link = root / "innocent.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows without symlink privilege: exercise the same rejection branch
        # with a synthetic link marker, without requiring elevated test rights.
        link.write_text("synthetic link target", encoding="utf-8")
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == link or original(p))
    destination = tmp_path / "candidate.zip"
    with pytest.raises(ValueError, match="link|reparse"):
        build_archive(root, destination)
