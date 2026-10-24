"""Master-key file configuration uses temporary synthetic keys only."""

from cryptography.fernet import Fernet
import pytest

from app.core import config, secret_box


def test_key_file_drives_runtime_and_production_validation(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("ascii")
    path = tmp_path / "master"
    path.write_text(key + "\n", encoding="utf-8")
    settings = config.settings
    monkeypatch.setattr(settings, "app_encryption_key", None)
    monkeypatch.setattr(settings, "app_encryption_key_file", str(path))
    monkeypatch.setattr(settings, "environment", "production")

    stored = secret_box.encrypt_secret("synthetic-provider-key")
    assert Fernet(key.encode()).decrypt(stored[len(secret_box.PREFIX):].encode()) == b"synthetic-provider-key"
    assert secret_box.decrypt_secret(stored) == "synthetic-provider-key"
    assert "encryption_key_not_configured" not in config.production_config_errors()


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid", "conflicting"])
def test_bad_key_file_fails_closed_without_exposing_path_or_content(monkeypatch, tmp_path, kind):
    path = tmp_path / "private-master-location"
    key = Fernet.generate_key().decode("ascii")
    if kind != "missing":
        path.write_text("" if kind == "empty" else "not-a-valid-master" if kind == "invalid" else key, encoding="utf-8")
    monkeypatch.setattr(config.settings, "environment", "production")
    monkeypatch.setattr(config.settings, "app_encryption_key_file", str(path))
    monkeypatch.setattr(config.settings, "app_encryption_key", Fernet.generate_key().decode() if kind == "conflicting" else None)

    with pytest.raises(secret_box.SecretBoxError) as captured:
        secret_box.encrypt_secret("synthetic-provider-key")
    assert str(path) not in str(captured.value)
    assert key not in str(captured.value)
    assert "not-a-valid-master" not in str(captured.value)
    assert "encryption_key_not_configured" in config.production_config_errors()


def test_identical_direct_and_file_values_are_accepted(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("ascii")
    path = tmp_path / "master"
    path.write_text(key, encoding="utf-8")
    monkeypatch.setattr(config.settings, "app_encryption_key", key)
    monkeypatch.setattr(config.settings, "app_encryption_key_file", str(path))

    assert secret_box.decrypt_secret(secret_box.encrypt_secret("synthetic-key")) == "synthetic-key"


def test_settings_repr_excludes_provider_keys_master_and_invites():
    private_fields = [name for name in config.Settings.model_fields if name.endswith("_api_key")]
    private_fields += ["app_encryption_key", "invite_tokens"]
    secrets = {name: "synthetic-private-" + name for name in private_fields}
    settings = config.Settings(_env_file=None, **secrets)

    rendered = repr(settings)
    assert not any(value in rendered for value in secrets.values())
