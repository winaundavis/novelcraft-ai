from app.core.config import Settings


def test_settings_load_novelcraft_database_path_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("NOVELCRAFT_DB_PATH", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("NOVELCRAFT_DB_PATH=../data/beta-user-001.db\n", encoding="utf-8")

    loaded = Settings(_env_file=env_file)

    assert loaded.novelcraft_db_path == "../data/beta-user-001.db"
