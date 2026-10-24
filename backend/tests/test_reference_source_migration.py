"""Reference source IDs remain readable metadata after source-project deletion."""

import sqlite3
from contextlib import closing

import pytest

from app import db
from app.dao import projects, reference_works
from scripts.backup_sqlite import backup_database, restore_database, verify_sqlite_database


def _snapshot(conn):
    return {
        "columns": [tuple(row) for row in conn.execute("PRAGMA table_info(reference_works)")],
        "rows": [tuple(row) for row in conn.execute("SELECT * FROM reference_works ORDER BY id")],
        "objects": [tuple(row) for row in conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='reference_works' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name")],
    }


@pytest.fixture
def legacy_database(monkeypatch, tmp_path):
    path = tmp_path / "legacy-reference.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    with closing(db.get_conn()) as conn, conn:
        # Force the old FK on a schema that also has owner/tags and an extra
        # payload column, regardless of the fresh-schema definition under test.
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='reference_works'").fetchone()[0]
        if not conn.execute("PRAGMA foreign_key_list(reference_works)").fetchall():
            end = sql.rfind(")")
            sql = sql[:end] + ", FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE" + sql[end:]
        conn.execute("DROP TABLE reference_works")
        conn.execute(sql)
        conn.execute("ALTER TABLE reference_works ADD COLUMN custom_payload TEXT NOT NULL DEFAULT 'unchanged'")
        conn.execute("CREATE UNIQUE INDEX idx_reference_custom ON reference_works(owner_id,name)")
        conn.execute("CREATE TRIGGER preserve_reference_name AFTER UPDATE OF source_note ON reference_works BEGIN UPDATE reference_works SET name=name WHERE id=NEW.id; END")
        conn.execute("INSERT INTO projects(id,name,style_config,created_at,updated_at) VALUES ('source-project','source','{}','now','now')")
        conn.execute("INSERT INTO reference_works(id,project_id,owner_id,name,raw_text,tags,created_at,updated_at,custom_payload) VALUES ('archive-a','source-project','local-dev','kept archive','synthetic prose','[\"tag\"]','now','now','custom-a')")
        conn.execute("INSERT INTO reference_works(id,project_id,owner_id,name,raw_text,created_at,updated_at,custom_payload) VALUES ('archive-b','already-deleted-source','local-dev','older archive','older prose','now','now','custom-b')")
    return path


def test_startup_migration_preserves_columns_rows_indexes_and_is_idempotent(legacy_database):
    with closing(db.get_conn()) as conn:
        before = _snapshot(conn)
    db.init_db()
    with closing(db.get_conn()) as conn:
        assert conn.execute("PRAGMA foreign_key_list(reference_works)").fetchall() == []
        assert _snapshot(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        migrated_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='reference_works'").fetchone()[0]
    db.init_db()
    with closing(db.get_conn()) as conn:
        assert _snapshot(conn) == before
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='reference_works'").fetchone()[0] == migrated_sql


def test_source_delete_preserves_readable_archives_and_strict_backup_restore(legacy_database, tmp_path):
    db.init_db()
    assert projects.delete_project("source-project") == 1
    archive = reference_works.get_reference_work("local-dev", "archive-a")
    assert archive["project_id"] == "source-project"
    assert archive["owner_id"] == "local-dev"
    assert archive["raw_text"] == "synthetic prose"
    with closing(db.get_conn()) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        expected = _snapshot(conn)
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    backup_database(legacy_database, backup, require_sqlite=True)
    restore_database(backup, restored, require_sqlite=True)
    assert verify_sqlite_database(restored)["integrity"] == "ok"
    with closing(sqlite3.connect(restored)) as conn:
        assert _snapshot(conn) == expected
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_failure_rolls_back_table_data_and_indexes(legacy_database):
    class FailBeforeRename(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('ALTER TABLE "reference_works_source_migration" RENAME'):
                raise sqlite3.OperationalError("synthetic migration failure")
            return super().execute(sql, parameters)

    with closing(sqlite3.connect(legacy_database, factory=FailBeforeRename)) as conn:
        conn.row_factory = sqlite3.Row
        before = _snapshot(conn)
        with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
            db._migrate_reference_works_source_metadata(conn)
        assert _snapshot(conn) == before
        assert conn.execute("PRAGMA foreign_key_list(reference_works)").fetchall()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='reference_works_source_migration'").fetchone() is None
