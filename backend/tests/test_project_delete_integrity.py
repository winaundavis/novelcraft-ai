"""Project deletion cleans owned records on fresh, synthetic SQLite databases."""

import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import db
from app.dao import projects
from main import app


def _seed_row(conn, table, project_id, chapter_id):
    """Populate required columns; defaults supply each table's optional payload."""
    values = {}
    for col in conn.execute(f'PRAGMA table_info("{table}")'):
        name = col["name"]
        if name == "id":
            values[name] = project_id + "-" + table
        elif name == "project_id":
            values[name] = project_id
        elif name == "chapter_id":
            values[name] = chapter_id
        elif col["notnull"] and col["dflt_value"] is None:
            values[name] = 1 if "INT" in col["type"] or "REAL" in col["type"] else "synthetic"
    if table == "novel_jobs":
        values["status"] = "completed"
    columns = ",".join('"' + name + '"' for name in values)
    conn.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


@pytest.mark.parametrize("entrypoint", ["api", "dao"])
def test_delete_project_cleans_owned_tables_and_preserves_other_project(monkeypatch, tmp_path, entrypoint):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "delete-project.sqlite3")
    db.init_db()
    with closing(db.get_conn()) as conn, conn:
        for pid in ("delete-target", "keep-project"):
            conn.execute("INSERT INTO projects(id,name,style_config,created_at,updated_at) VALUES (?,?, '{}','now','now')", (pid, pid))
            conn.execute("INSERT INTO chapters(id,project_id,title,created_at,updated_at) VALUES (?,?,?,'now','now')", (pid + "-chapter", pid, pid))
        # Discover owned tables from the schema so adding a table cannot silently
        # bypass this integrity check. Personal reference archives are retained.
        tables = []
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            table = row["name"]
            if table not in {"chapters", "reference_works"} and any(col["name"] == "project_id" for col in conn.execute(f'PRAGMA table_info("{table}")')):
                tables.append(table)
        for pid in ("delete-target", "keep-project"):
            for table in tables:
                _seed_row(conn, table, pid, pid + "-chapter")
        tables.append("chapters")
        preserved = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" WHERE project_id=?', ("keep-project",))] for table in tables}
        # A personal archive retains its source project marker under the existing contract.
        conn.execute("INSERT INTO reference_works(id,project_id,owner_id,name,raw_text,created_at,updated_at) VALUES ('personal-archive','delete-target','local-dev','archive','text','now','now')")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    if entrypoint == "api":
        with TestClient(app) as client:
            assert client.delete("/api/projects/delete-target").status_code == 200
    else:
        assert projects.delete_project("delete-target") == 1

    with closing(db.get_conn()) as conn:
        remaining = {table: conn.execute(f'SELECT count(*) FROM "{table}" WHERE project_id=?', ("delete-target",)).fetchone()[0] for table in tables}
        assert not any(remaining.values()), remaining
        for table, expected in preserved.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" WHERE project_id=?', ("keep-project",))] == expected
        assert conn.execute("SELECT count(*) FROM projects WHERE id='keep-project'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM reference_works WHERE id='personal-archive'").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
