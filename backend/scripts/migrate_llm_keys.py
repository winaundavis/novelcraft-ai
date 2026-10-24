"""Encrypt plaintext provider credentials in one explicitly selected SQLite DB.

This tool never guesses the database path and never prints credentials. Make a
separate backup before running it. It is intentionally not part of startup.
Stop all database users first: a successful write also vacuums the database and
truncates its WAL, including when credentials were already encrypted.
From backend/: python -m scripts.migrate_llm_keys <explicit-database> [--dry-run]
Supply APP_ENCRYPTION_KEY or APP_ENCRYPTION_KEY_FILE through the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import resolve_encryption_key
from app.core.secret_box import PREFIX


def migrate(database: Path, key: str, *, dry_run: bool = False) -> int:
    if not database.is_file():
        raise FileNotFoundError(database)
    resolved_key = resolve_encryption_key(key)
    if not resolved_key:
        raise ValueError("APP_ENCRYPTION_KEY is required")
    box = Fernet(resolved_key.encode("ascii"))

    def protect(value: str | None) -> tuple[str, int]:
        stored = str(value or "")
        if not stored:
            return stored, 0
        if stored.startswith(PREFIX):
            try:
                box.decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeError, ValueError):
                raise ValueError("stored credential cannot be decrypted with the supplied key") from None
            return stored, 0
        return PREFIX + box.encrypt(stored.encode("utf-8")).decode("ascii"), 1

    changed = 0
    uri = database.resolve().as_uri() + ("?mode=ro" if dry_run else "?mode=rw")
    with closing(sqlite3.connect(uri, uri=True)) as conn, conn:
        if not dry_run:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            if journal_mode not in {"delete", "wal"}:
                raise ValueError("physical cleanup supports journal modes DELETE and WAL only; no credentials were changed")
            conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, api_key, role_overrides FROM llm_configs").fetchall()
        updates: list[tuple[str, str, str]] = []
        for config_id, api_key, role_json in rows:
            new_key, key_changed = protect(api_key)
            try:
                roles = json.loads(role_json)
            except (TypeError, ValueError):
                raise ValueError("stored role_overrides is invalid JSON") from None
            if not isinstance(roles, dict):
                raise ValueError("stored role_overrides must be a JSON object")
            role_changes = 0
            for cfg in roles.values():
                if not isinstance(cfg, dict):
                    raise ValueError("stored role_overrides entries must be JSON objects")
                if cfg.get("api_key"):
                    cfg["api_key"], count = protect(cfg["api_key"])
                    role_changes += count
            new_roles = json.dumps(roles, ensure_ascii=False) if role_changes else role_json
            if key_changed or role_changes:
                updates.append((new_key, new_roles, config_id))
                changed += key_changed + role_changes
        if not dry_run:
            conn.executemany("UPDATE llm_configs SET api_key = ?, role_overrides = ? WHERE id = ?", updates)
            conn.commit()
            # VACUUM must run outside the logical migration transaction. It is
            # necessary even with zero updates to remove historical free pages.
            try:
                conn.execute("VACUUM")
                if journal_mode == "wal":
                    checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint != (0, 0, 0):
                        raise sqlite3.OperationalError("WAL checkpoint is busy or incomplete")
            except sqlite3.Error:
                raise sqlite3.OperationalError(
                    "Physical SQLite cleanup is busy or failed. Logical credential encryption may already be committed. "
                    "Stop all database users and rerun this command to finish cleanup."
                ) from None
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt NovelCraft SQLite LLM credentials")
    parser.add_argument("database", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        key = resolve_encryption_key(os.environ.get("APP_ENCRYPTION_KEY"), os.environ.get("APP_ENCRYPTION_KEY_FILE"))
        if not key:
            raise ValueError("APP_ENCRYPTION_KEY or APP_ENCRYPTION_KEY_FILE must be supplied out of band")
        count = migrate(args.database, key, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"database": str(args.database), "changed": count, "dryRun": args.dry_run}))


if __name__ == "__main__":
    main()
