"""Application-layer protection for provider credentials stored in SQLite.

The encryption key is supplied out of band through ``APP_ENCRYPTION_KEY`` or
the private file selected by ``APP_ENCRYPTION_KEY_FILE``.
Development fixtures may continue to use legacy plaintext values so existing
local projects remain readable; production readiness rejects those values.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import is_production_environment, resolve_encryption_key, settings

PREFIX = "enc:v1:"


class SecretBoxError(ValueError):
    """Raised when a credential cannot be protected or recovered."""


def _fernet() -> Fernet | None:
    try:
        key = resolve_encryption_key(settings.app_encryption_key, settings.app_encryption_key_file)
        return Fernet(key.encode("ascii")) if key else None
    except ValueError as exc:
        raise SecretBoxError(str(exc)) from None


def encrypt_secret(value: str | None) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if raw.startswith(PREFIX):
        return raw
    box = _fernet()
    if box is None:
        if is_production_environment():
            raise SecretBoxError("APP_ENCRYPTION_KEY is required in production")
        return raw
    return PREFIX + box.encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    raw = str(value or "")
    if not raw:
        return raw
    if not raw.startswith(PREFIX):
        if is_production_environment():
            raise SecretBoxError("stored credential is not encrypted")
        return raw
    box = _fernet()
    if box is None:
        raise SecretBoxError("APP_ENCRYPTION_KEY is required to read an encrypted credential")
    try:
        return box.decrypt(raw[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise SecretBoxError("stored credential cannot be decrypted") from exc


def is_protected(value: str | None) -> bool:
    return bool(value and str(value).startswith(PREFIX))
