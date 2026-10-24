import sqlite3
from unittest.mock import patch

from app import db


def test_active_llm_config_is_absent_before_fresh_database_schema_exists():
    with patch("app.dao.llm_configs.get_active_config", side_effect=sqlite3.OperationalError("no such table: llm_configs")):
        assert db.get_active_llm_config() is None
