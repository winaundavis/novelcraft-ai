"""Consistent SQLite backup/restore helper.

The public function signatures intentionally keep the original positional
arguments.  Valid SQLite files use SQLite's online backup API and are checked
with ``PRAGMA integrity_check``.  A small raw-copy fallback remains available
for legacy callers that used this helper with arbitrary files; the command
line entry point always enables strict SQLite validation.
"""

import argparse
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path


SQLITE_HEADER = b"SQLite format 3\x00"


def _is_sqlite_file(path: Path) -> bool:
    """Return whether *path* has SQLite's file signature."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _integrity_check(path: Path, *, required_tables: tuple[str, ...] = ()) -> None:
    """Validate a SQLite file without exposing its contents."""
    if not _is_sqlite_file(path):
        raise ValueError(f"not a SQLite database: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise ValueError(f"SQLite integrity check failed: {path}")
        foreign = conn.execute("PRAGMA foreign_key_check").fetchone()
        if foreign is not None:
            raise ValueError(f"SQLite foreign-key check failed: {path}")
        if required_tables:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            names = {str(row[0]) for row in rows}
            missing = [name for name in required_tables if name not in names]
            if missing:
                raise ValueError(
                    "SQLite database is missing required tables: " + ", ".join(missing)
                )
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"SQLite integrity check failed: {path}") from exc
    finally:
        # sqlite3.Connection.__exit__ only commits/rolls back; it does not
        # close the handle.  Explicitly close it so Windows can atomically
        # replace temporary backup files after validation.
        conn.close()


def verify_sqlite_database(path: Path, *, required_tables: tuple[str, ...] = ()) -> dict[str, object]:
    """Validate a database and return content-free verification metadata."""
    path = Path(path)
    _integrity_check(path, required_tables=required_tables)
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    finally:
        conn.close()
    return {
        "path": str(path),
        "integrity": "ok",
        "page_count": page_count,
        "schema_version": schema_version,
    }


def _online_backup(source: Path, destination: Path) -> None:
    """Copy a SQLite database consistently, including committed WAL pages."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
        # SQLite's backup API copies the source journal mode into the target.
        # A WAL target leaves ``-wal``/``-shm`` sidecars behind, which can keep
        # the main file locked on Windows and make the subsequent atomic
        # replace fail.  Backups are standalone snapshots, so normalize them
        # to rollback-journal mode before closing the connection.
        destination_conn.execute("PRAGMA journal_mode=DELETE")
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()


def _backup_to_temp(source: Path, parent: Path, prefix: str) -> Path:
    """Create a validated temporary SQLite backup in *parent*."""
    fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".db", dir=parent)
    os.close(fd)
    temporary = Path(raw_path)
    try:
        temporary.unlink()
        _online_backup(source, temporary)
        _integrity_check(temporary)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def backup_database(database: Path, archive: Path, *, require_sqlite: bool = False) -> Path:
    if not database.is_file():
        raise FileNotFoundError(f"source database not found: {database}")
    if archive.exists():
        raise FileExistsError(f"backup destination already exists: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not _is_sqlite_file(database):
        if require_sqlite:
            raise ValueError(f"source is not a SQLite database: {database}")
        # Compatibility with the original helper, whose tests and callers also
        # used it as a generic protected file copy.
        shutil.copy2(database, archive)
        return archive
    temporary = _backup_to_temp(database, archive.parent, ".novelcraft-backup-")
    try:
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    _integrity_check(archive)
    return archive


def restore_database(
    archive: Path,
    database: Path,
    *,
    replace: bool = False,
    require_sqlite: bool = False,
) -> Path | None:
    if not archive.is_file():
        raise FileNotFoundError(f"backup database not found: {archive}")
    archive_is_sqlite = _is_sqlite_file(archive)
    if require_sqlite and not archive_is_sqlite:
        raise ValueError(f"backup is not a SQLite database: {archive}")
    preserved: Path | None = None
    if database.exists():
        if not replace:
            raise FileExistsError("target database exists; use --replace after making a backup")
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        preserved = database.with_name(f"{database.name}.pre-restore-{stamp}.bak")
        if _is_sqlite_file(database):
            _online_backup(database, preserved)
            _integrity_check(preserved)
        else:
            shutil.copy2(database, preserved)
    database.parent.mkdir(parents=True, exist_ok=True)
    if not archive_is_sqlite:
        shutil.copy2(archive, database)
        return preserved

    temporary = _backup_to_temp(archive, database.parent, ".novelcraft-restore-")
    try:
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    _integrity_check(database)
    return preserved


def main() -> None:
    parser = argparse.ArgumentParser(description="NovelCraft SQLite manual backup/restore")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("database", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--replace", action="store_true", help="restore only: preserve and replace an existing database")
    args = parser.parse_args()
    try:
        if args.action == "backup":
            backup_database(args.database, args.archive, require_sqlite=True)
        else:
            preserved = restore_database(
                args.archive, args.database, replace=args.replace, require_sqlite=True
            )
            if preserved:
                print(f"preserved current database: {preserved}")
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
