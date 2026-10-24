"""Credential-at-rest checks use only generated in-memory keys."""

from cryptography.fernet import Fernet
import pytest

from app.core import secret_box
from app.core.config import settings


def test_secret_box_round_trip_and_idempotent(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "app_encryption_key", key)
    monkeypatch.setattr(settings, "environment", "development")

    stored = secret_box.encrypt_secret("synthetic-provider-key")
    assert stored.startswith(secret_box.PREFIX)
    assert secret_box.decrypt_secret(stored) == "synthetic-provider-key"
    assert secret_box.encrypt_secret(stored) == stored


def test_production_rejects_plaintext_when_encryption_key_missing(monkeypatch):
    monkeypatch.setattr(settings, "app_encryption_key", None)
    monkeypatch.setattr(settings, "environment", "production")

    with pytest.raises(secret_box.SecretBoxError):
        secret_box.encrypt_secret("synthetic-provider-key")


def test_wrong_key_cannot_decrypt(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    other = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "app_encryption_key", key)
    stored = secret_box.encrypt_secret("synthetic-provider-key")
    monkeypatch.setattr(settings, "app_encryption_key", other)

    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt_secret(stored)


def test_production_cannot_read_legacy_plaintext(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode("ascii"))

    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt_secret("synthetic-provider-key")
    assert secret_box.decrypt_secret("") == ""


def test_development_can_read_legacy_plaintext(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "app_encryption_key", None)

    assert secret_box.decrypt_secret("synthetic-provider-key") == "synthetic-provider-key"
