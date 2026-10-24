"""P2-2b 写作冲突（novel_facts is_conflict=1）—— DAO 删除 + API 查询/处理单测。

覆盖：
- DAO：delete_writing_conflict 删除冲突记录 / 不误删正常事实 / 不存在或非冲突返回 False
- API：GET /api/projects/{id}/conflicts 返回新→旧列表（含 summary/rootCause 拆分）；
  PATCH /api/projects/{id}/conflicts/{fact_id} 标记已处理（删除）；404 路径。

数据库用独立内存 SQLite 隔离，TestClient 处理请求时 patch 两个 DAO 的 get_conn。
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.dao import novel_facts as novel_facts_dao
from app.dao import projects as projects_dao
from main import app


PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    genre TEXT NOT NULL DEFAULT 'xuanhuan',
    target_word_count INTEGER NOT NULL DEFAULT 300000,
    current_word_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'planning',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    style_config TEXT NOT NULL DEFAULT '{}',
    outline TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    is_starred INTEGER NOT NULL DEFAULT 0,
    premise TEXT NOT NULL DEFAULT '',
    target_chapters INTEGER NOT NULL DEFAULT 30,
    characters TEXT NOT NULL DEFAULT '[]',
    locations TEXT NOT NULL DEFAULT '[]',
    world_rules TEXT NOT NULL DEFAULT '[]',
    import_analysis TEXT,
    inspiration_fragments TEXT NOT NULL DEFAULT '[]'
);
"""

NOVEL_FACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS novel_facts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    fact_type TEXT NOT NULL DEFAULT 'completed',
    content TEXT NOT NULL,
    chapter_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    is_conflict INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, content)
);
"""


def _build_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PROJECTS_SCHEMA + NOVEL_FACTS_SCHEMA)
    conn.commit()
    return conn


@pytest.fixture
def memory_db():
    """DAO 级隔离库：仅 novel_facts 表 + patch novel_facts_dao.get_conn。"""
    conn = _build_memory_db()

    def _get_conn():
        return conn

    with patch.object(novel_facts_dao, "get_conn", _get_conn):
        yield conn
    conn.close()


@pytest.fixture
def api_env():
    """API 级隔离库：patch projects_dao + novel_facts_dao 的 get_conn。"""
    conn = _build_memory_db()

    def _get_conn():
        return conn

    with patch.object(projects_dao, "get_conn", _get_conn), patch.object(
        novel_facts_dao, "get_conn", _get_conn
    ):
        yield conn
    conn.close()


PROJECT_ID = "proj-conflict-1"
OTHER_PROJECT_ID = "proj-conflict-2"


def _insert_project(conn: sqlite3.Connection, project_id: str = PROJECT_ID) -> None:
    conn.execute(
        """
        INSERT INTO projects
            (id, name, genre, status, created_at, updated_at, style_config, target_chapters)
        VALUES (?, '冲突测试项目', 'xuanhuan', 'planning', '2026-08-10T00:00:00Z',
                '2026-08-10T00:00:00Z', '{}', 30)
        """,
        (project_id,),
    )
    conn.commit()


def _add_conflict(
    conn: sqlite3.Connection,
    project_id: str = PROJECT_ID,
    *,
    summary: str = "主角身份前后矛盾",
    root_cause: str = "第三章改写后未同步设定",
    chapter_id: str = "ch-3",
    fact_id: str | None = None,
    created_at: str = "2026-08-10T00:00:00Z",
) -> str:
    content = f"{summary}（根因：{root_cause}）" if root_cause else summary
    fid = fact_id or f"conf-{summary[:4]}"
    conn.execute(
        """
        INSERT INTO novel_facts
            (id, project_id, fact_type, content, chapter_id, created_at, is_conflict)
        VALUES (?, ?, 'conflict', ?, ?, ?, 1)
        """,
        (fid, project_id, content, chapter_id, created_at),
    )
    conn.commit()
    return fid


# ═══════════════════════════════════════════════════════════
# DAO：delete_writing_conflict
# ═══════════════════════════════════════════════════════════

class TestDeleteWritingConflictDao:
    def test_deletes_conflict_record(self, memory_db):
        fid = _add_conflict(memory_db)
        assert novel_facts_dao.count_facts(PROJECT_ID) == 1
        assert novel_facts_dao.delete_writing_conflict(PROJECT_ID, fid) is True
        assert novel_facts_dao.count_facts(PROJECT_ID) == 0

    def test_does_not_delete_normal_fact(self, memory_db):
        novel_facts_dao.add_fact(PROJECT_ID, "completed", "主角取得青云令", "ch-1")
        fid = novel_facts_dao.list_facts(PROJECT_ID)[0]["id"]
        assert novel_facts_dao.delete_writing_conflict(PROJECT_ID, fid) is False
        assert novel_facts_dao.count_facts(PROJECT_ID) == 1

    def test_returns_false_when_missing_or_wrong_project(self, memory_db):
        fid = _add_conflict(memory_db)
        assert novel_facts_dao.delete_writing_conflict(PROJECT_ID, "not-exist") is False
        assert novel_facts_dao.delete_writing_conflict(OTHER_PROJECT_ID, fid) is False
        assert novel_facts_dao.count_facts(PROJECT_ID) == 1

    def test_list_conflicts_excludes_normal_facts(self, memory_db):
        novel_facts_dao.add_fact(PROJECT_ID, "completed", "正常事实", "ch-1")
        _add_conflict(memory_db, summary="冲突A", fact_id="conf-A", created_at="2026-08-10T00:00:00Z")
        _add_conflict(memory_db, summary="冲突B", fact_id="conf-B", created_at="2026-08-10T01:00:00Z")
        conflicts = novel_facts_dao.list_writing_conflicts(PROJECT_ID)
        assert len(conflicts) == 2
        assert all(row["is_conflict"] == 1 for row in conflicts)
        # 新→旧：后插入（created_at 更大）的在前
        assert conflicts[0]["id"] == "conf-B"
        assert conflicts[1]["id"] == "conf-A"


# ═══════════════════════════════════════════════════════════
# API：GET /api/projects/{id}/conflicts
# ═══════════════════════════════════════════════════════════

class TestListConflictsApi:
    def test_list_returns_split_fields(self, api_env):
        _insert_project(api_env)
        _add_conflict(api_env, summary="主角身份前后矛盾", root_cause="设定未同步", chapter_id="ch-3")
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{PROJECT_ID}/conflicts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert isinstance(data, list)
        assert len(data) == 1
        item = data[0]
        assert item["projectId"] == PROJECT_ID
        assert item["summary"] == "主角身份前后矛盾"
        assert item["rootCause"] == "设定未同步"
        assert item["chapterId"] == "ch-3"
        assert item["createdAt"]
        # content 保留完整落盘文本
        assert "（根因：" in item["content"]

    def test_list_without_root_cause_keeps_full_content(self, api_env):
        _insert_project(api_env)
        _add_conflict(api_env, summary="无根因冲突", root_cause="", chapter_id="ch-4", fact_id="conf-no-root")
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{PROJECT_ID}/conflicts")
        item = resp.json()["data"][0]
        assert item["summary"] == "无根因冲突"
        assert item["rootCause"] == ""
        assert item["content"] == "无根因冲突"

    def test_list_empty_when_no_conflicts(self, api_env):
        _insert_project(api_env)
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{PROJECT_ID}/conflicts")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_list_project_not_found_returns_404(self, api_env):
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{OTHER_PROJECT_ID}/conflicts")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════
# API：PATCH /api/projects/{id}/conflicts/{fact_id}（标记已处理）
# ═══════════════════════════════════════════════════════════

class TestResolveConflictApi:
    def test_resolve_deletes_conflict(self, api_env):
        _insert_project(api_env)
        fid = _add_conflict(api_env)
        with TestClient(app) as client:
            resp = client.patch(f"/api/projects/{PROJECT_ID}/conflicts/{fid}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert novel_facts_dao.list_writing_conflicts(PROJECT_ID) == []

    def test_resolve_not_found_returns_404(self, api_env):
        _insert_project(api_env)
        with TestClient(app) as client:
            resp = client.patch(f"/api/projects/{PROJECT_ID}/conflicts/not-exist")
        assert resp.status_code == 404

    def test_resolve_project_not_found_returns_404(self, api_env):
        with TestClient(app) as client:
            resp = client.patch(f"/api/projects/{OTHER_PROJECT_ID}/conflicts/any")
        assert resp.status_code == 404

    def test_resolve_does_not_touch_normal_facts(self, api_env):
        _insert_project(api_env)
        novel_facts_dao.add_fact(PROJECT_ID, "completed", "正常事实", "ch-1")
        fid = _add_conflict(api_env)
        with TestClient(app) as client:
            resp = client.patch(f"/api/projects/{PROJECT_ID}/conflicts/{fid}")
        assert resp.status_code == 200
        assert novel_facts_dao.count_facts(PROJECT_ID) == 1
        assert novel_facts_dao.list_facts(PROJECT_ID)[0]["content"] == "正常事实"
