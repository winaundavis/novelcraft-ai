"""SQLite backup/recovery checks using isolated temporary databases only."""

import sqlite3

import pytest

from scripts.backup_sqlite import backup_database, restore_database, verify_sqlite_database


def _make_database(path, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO entries(value) VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _read_value(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("SELECT value FROM entries ORDER BY id").fetchone()[0])
    finally:
        conn.close()


def test_online_backup_preserves_sqlite_rows_and_passes_integrity(tmp_path):
    database = tmp_path / "data" / "novelcraft.db"
    archive = tmp_path / "backup" / "novelcraft.db"
    database.parent.mkdir()
    _make_database(database, "before")

    backup_database(database, archive, require_sqlite=True)

    assert _read_value(archive) == "before"
    metadata = verify_sqlite_database(archive, required_tables=("entries",))
    assert metadata["integrity"] == "ok"
    assert metadata["page_count"] > 0


def test_restore_validates_archive_and_preserves_previous_database(tmp_path):
    source = tmp_path / "source.db"
    archive = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _make_database(source, "original")
    backup_database(source, archive, require_sqlite=True)
    _make_database(target, "current")

    preserved = restore_database(archive, target, replace=True, require_sqlite=True)

    assert preserved is not None and preserved.exists()
    assert _read_value(preserved) == "current"
    assert _read_value(target) == "original"
    verify_sqlite_database(target, required_tables=("entries",))


def test_strict_backup_and_restore_reject_non_sqlite_files(tmp_path):
    source = tmp_path / "source.bin"
    archive = tmp_path / "backup.db"
    source.write_bytes(b"not a database")

    with pytest.raises(ValueError):
        backup_database(source, archive, require_sqlite=True)

    archive.write_bytes(b"not a database")
    target = tmp_path / "target.db"
    with pytest.raises(ValueError):
        restore_database(archive, target, require_sqlite=True)


def test_legacy_raw_copy_compatibility_remains_available(tmp_path):
    source = tmp_path / "source.bin"
    archive = tmp_path / "backup.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"legacy")

    backup_database(source, archive)
    restore_database(archive, target)

    assert target.read_bytes() == b"legacy"
