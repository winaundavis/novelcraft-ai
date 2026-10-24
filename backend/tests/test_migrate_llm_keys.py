"""Credential migration uses synthetic keys and explicitly selected temporary DBs."""

import json
import sqlite3
import sys
import uuid
from contextlib import closing, contextmanager

from cryptography.fernet import Fernet
import pytest

from app.core.secret_box import PREFIX
from scripts.migrate_llm_keys import main, migrate


def _database(tmp_path, rows):
    path = tmp_path / "credentials.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE llm_configs (id TEXT PRIMARY KEY, api_key TEXT, role_overrides TEXT)")
        conn.executemany("INSERT INTO llm_configs VALUES (?, ?, ?)", rows)
    return path


def _rows(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT id, api_key, role_overrides FROM llm_configs ORDER BY id").fetchall()


def test_migration_counts_credentials_preserves_content_and_is_idempotent(tmp_path):
    key = Fernet.generate_key()
    roles = '{ "writing": {"api_key": "synthetic-role-key", "model": "model-a"}, "check": {"temperature": 0.25} }'
    path = _database(tmp_path, [("one", "synthetic-main-key", roles), ("two", "", '{ "writing": {} }')])

    assert migrate(path, key.decode("ascii"), dry_run=True) == 2
    assert _rows(path)[0] == ("one", "synthetic-main-key", roles)
    assert migrate(path, key.decode("ascii")) == 2
    stored = _rows(path)
    box = Fernet(key)
    assert box.decrypt(stored[0][1][len(PREFIX):].encode()).decode() == "synthetic-main-key"
    saved_roles = json.loads(stored[0][2])
    assert box.decrypt(saved_roles["writing"]["api_key"][len(PREFIX):].encode()).decode() == "synthetic-role-key"
    assert saved_roles["writing"]["model"] == "model-a"
    assert saved_roles["check"] == {"temperature": 0.25}
    assert stored[1] == ("two", "", '{ "writing": {} }')
    assert migrate(path, key.decode("ascii")) == 0
    assert _rows(path) == stored
    # A successful migration releases its transaction and connection.
    with closing(sqlite3.connect(path, timeout=0)) as conn:
        conn.execute("BEGIN EXCLUSIVE")


@pytest.mark.parametrize("role_json", ['{"writing":', '[]', 'null', '"not-an-object"'])
def test_migration_rejects_invalid_roles_without_changing_any_row(tmp_path, role_json):
    original = [("one", "synthetic-first-key", "{}"), ("two", "synthetic-second-key", role_json)]
    path = _database(tmp_path, original)

    with pytest.raises(ValueError):
        migrate(path, Fernet.generate_key().decode("ascii"))

    assert _rows(path) == original


@pytest.mark.parametrize("role_credential", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_migration_rejects_wrong_master_for_existing_ciphertext(tmp_path, role_credential, dry_run):
    encrypted = PREFIX + Fernet(Fernet.generate_key()).encrypt(b"synthetic-existing-key").decode()
    row = ("two", "" if role_credential else encrypted,
           json.dumps({"writing": {"api_key": encrypted}}) if role_credential else "{}")
    original = [("one", "synthetic-first-key", "{}"), row]
    path = _database(tmp_path, original)

    with pytest.raises(ValueError):
        migrate(path, Fernet.generate_key().decode("ascii"), dry_run=dry_run)

    assert _rows(path) == original


@pytest.mark.parametrize("key", ["", "invalid-master-key"])
def test_migration_rejects_missing_or_invalid_master_without_writes(tmp_path, key):
    original = [("one", "synthetic-key", "{}")]
    path = _database(tmp_path, original)

    with pytest.raises(ValueError):
        migrate(path, key)

    assert _rows(path) == original


def test_migration_never_creates_a_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        migrate(path, Fernet.generate_key().decode("ascii"))
    assert not path.exists()


def test_cli_reads_master_file_without_printing_it(monkeypatch, tmp_path, capsys):
    key = Fernet.generate_key()
    key_file = tmp_path / "private-master"
    key_file.write_text(key.decode("ascii"), encoding="utf-8")
    path = _database(tmp_path, [("one", "synthetic-key", "{}")])
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "")
    monkeypatch.setenv("APP_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.setattr(sys, "argv", ["migrate_llm_keys", str(path)])

    main()

    output = capsys.readouterr().out
    assert json.loads(output)["changed"] == 1
    assert key.decode("ascii") not in output
    assert str(key_file) not in output
    assert Fernet(key).decrypt(_rows(path)[0][1][len(PREFIX):].encode()) == b"synthetic-key"


def _storage_bytes(path):
    wal = path.with_name(path.name + "-wal")
    return {"database": path.read_bytes(), "wal": wal.read_bytes() if wal.exists() else None}


@contextmanager
def _physical_database(tmp_path, journal_mode, *, encrypted_with_history=False):
    key = Fernet.generate_key()
    markers = [f"simulated-{kind}-{uuid.uuid4().hex}-" for kind in ("main", "role")]
    # Repeated unique markers fill overflow pages. Encryption grows the record
    # enough to move cells, leaving recoverable bytes when secure_delete is off.
    main_key, role_key = (marker * 160 for marker in markers)
    roles = json.dumps({"writing": {"api_key": role_key, "model": "fake-model"}})
    path = _database(tmp_path, [("active", main_key, roles)])
    with closing(sqlite3.connect(path)) as keeper:
        assert keeper.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0].upper() == journal_mode
        keeper.execute("PRAGMA secure_delete=OFF")
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        if encrypted_with_history:
            encrypted_main = PREFIX + Fernet(key).encrypt(main_key.encode()).decode()
            encrypted_roles = json.dumps({"writing": {
                "api_key": PREFIX + Fernet(key).encrypt(role_key.encode()).decode(), "model": "fake-model",
            }})
            with keeper:
                keeper.execute("UPDATE llm_configs SET api_key=?, role_overrides=? WHERE id='active'", (encrypted_main, encrypted_roles))
                keeper.execute("INSERT INTO llm_configs VALUES ('retired', ?, ?)", (main_key, roles))
            with keeper:
                keeper.execute("DELETE FROM llm_configs WHERE id='retired'")
        elif journal_mode == "WAL":
            # Put the same current credentials in both the main DB and WAL.
            with keeper:
                keeper.execute("UPDATE llm_configs SET api_key = api_key || '-current'")
        yield path, key.decode(), [marker.encode() for marker in markers], keeper


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
@pytest.mark.parametrize("encrypted_with_history", [False, True], ids=["current-plaintext", "encrypted-with-history"])
def test_physical_cleanup_removes_current_and_historical_plaintext(tmp_path, journal_mode, encrypted_with_history):
    with _physical_database(tmp_path, journal_mode, encrypted_with_history=encrypted_with_history) as (path, key, markers, keeper):
        before = _storage_bytes(path)
        assert all(marker in before["database"] for marker in markers)
        if journal_mode == "WAL":
            assert all(marker in before["wal"] for marker in markers)
        original = _rows(path)

        assert migrate(path, key) == (0 if encrypted_with_history else 2)

        after = _storage_bytes(path)
        for content in after.values():
            assert content is None or all(marker not in content for marker in markers)
        assert keeper.execute("PRAGMA journal_mode").fetchone()[0].upper() == journal_mode
        stored = _rows(path)
        if encrypted_with_history:
            assert stored == original
        else:
            box = Fernet(key.encode())
            assert box.decrypt(stored[0][1][len(PREFIX):].encode()).decode() == original[0][1]
            saved_roles = json.loads(stored[0][2])
            original_roles = json.loads(original[0][2])
            assert box.decrypt(saved_roles["writing"]["api_key"][len(PREFIX):].encode()).decode() == original_roles["writing"]["api_key"]


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_dry_run_uses_read_only_connection_and_preserves_database_and_wal_bytes(tmp_path, monkeypatch, journal_mode):
    with _physical_database(tmp_path, journal_mode) as (path, key, _, _keeper):
        before = _storage_bytes(path)
        connect = sqlite3.connect
        opened = []

        def tracked_connect(database, *args, **kwargs):
            opened.append(str(database))
            return connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", tracked_connect)
        assert migrate(path, key, dry_run=True) == 2
        assert opened == [path.resolve().as_uri() + "?mode=ro"]
        assert _storage_bytes(path) == before


def test_write_connection_enables_secure_delete(tmp_path, monkeypatch):
    path = _database(tmp_path, [("active", "synthetic-key", "{}")])
    connect = sqlite3.connect
    observed = []

    class TrackedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.execute("PRAGMA secure_delete=OFF")

        def close(self):
            observed.append(self.execute("PRAGMA secure_delete").fetchone()[0])
            super().close()

    def tracked_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=TrackedConnection)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    assert migrate(path, Fernet.generate_key().decode()) == 1
    assert observed == [1]


def _short_lock_timeout(monkeypatch):
    connect = sqlite3.connect

    def fast_connect(*args, **kwargs):
        kwargs["timeout"] = 0.01
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fast_connect)


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_existing_writer_lock_refuses_migration_without_logical_changes(tmp_path, monkeypatch, journal_mode):
    with _physical_database(tmp_path, journal_mode) as (path, key, _, _keeper):
        original = _rows(path)
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            _short_lock_timeout(monkeypatch)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                migrate(path, key)
            assert _rows(path) == original


def test_wal_reader_lock_reports_committed_encryption_and_allows_cleanup_retry(tmp_path, monkeypatch):
    with _physical_database(tmp_path, "WAL", encrypted_with_history=True) as (path, key, markers, _keeper):
        # Add a current plaintext key after seeding encrypted historical data.
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute("UPDATE llm_configs SET api_key = ? WHERE id='active'", (markers[0].decode() * 160,))
        with closing(sqlite3.connect(path)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT api_key FROM llm_configs").fetchall()
            _short_lock_timeout(monkeypatch)

            with pytest.raises(sqlite3.OperationalError, match="may already be committed.*Stop all database users"):
                migrate(path, key)

            assert _rows(path)[0][1].startswith(PREFIX)
            assert any(marker in content for content in _storage_bytes(path).values() if content for marker in markers)

        # This second run has no logical changes, but must finish physical work.
        assert migrate(path, key) == 0
        for content in _storage_bytes(path).values():
            assert content is None or all(marker not in content for marker in markers)
