def test_test_database_path_is_explicit_and_schema_is_initialized():
    from app import db

    assert "novelcraft-pytest-" in str(db.DB_PATH)
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_configs'").fetchone()
