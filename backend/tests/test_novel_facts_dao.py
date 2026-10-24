"""novel_facts（M1 事实账本）DAO 单元测试 —— 使用 SQLite 内存数据库。

覆盖：
- add_fact 正常写入 / 幂等去重（project_id + content 唯一）
- 空内容跳过、非法 fact_type 归一到 completed
- list_facts / list_completed_revealed_facts / list_recent_state_changed_facts
- count_facts / delete_facts_by_project
"""

from __future__ import annotations

import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from app.dao import novel_facts as novel_facts_dao


@pytest.fixture
def memory_db():
    """独立 SQLite 内存数据库，建 novel_facts 表并 patch DAO 的 get_conn。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
    )
    conn.commit()

    def _get_conn():
        return conn

    with patch.object(novel_facts_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


# ── add_fact 正常路径 ───────────────────────────────────

def test_add_fact_inserts_new_fact(memory_db):
    inserted = novel_facts_dao.add_fact(
        project_id="proj-1", fact_type="completed", content="主角取得了青云令", chapter_id="ch-1"
    )
    assert inserted is True
    assert novel_facts_dao.count_facts("proj-1") == 1


def test_add_fact_is_idempotent_on_same_content(memory_db):
    """同一 project 相同 content 重复写入：第二次被唯一索引忽略，不产生重复行。"""
    first = novel_facts_dao.add_fact(
        project_id="proj-1", fact_type="completed", content="主角取得青云令", chapter_id="ch-1"
    )
    second = novel_facts_dao.add_fact(
        project_id="proj-1", fact_type="revealed", content="主角取得青云令", chapter_id="ch-2"
    )
    assert first is True
    assert second is False
    assert novel_facts_dao.count_facts("proj-1") == 1


def test_add_fact_same_content_across_projects_allowed(memory_db):
    """唯一索引作用域是 (project_id, content)，不同项目允许相同内容。"""
    novel_facts_dao.add_fact("proj-1", "completed", "主角取得青云令", "ch-1")
    novel_facts_dao.add_fact("proj-2", "completed", "主角取得青云令", "ch-1")
    assert novel_facts_dao.count_facts("proj-1") == 1
    assert novel_facts_dao.count_facts("proj-2") == 1


def test_add_fact_skips_empty_content(memory_db):
    inserted = novel_facts_dao.add_fact("proj-1", "completed", "   ", "ch-1")
    assert inserted is False
    assert novel_facts_dao.count_facts("proj-1") == 0


def test_add_fact_normalizes_invalid_type(memory_db):
    inserted = novel_facts_dao.add_fact("proj-1", "unknown_type", "某事件", "ch-1")
    assert inserted is True
    facts = novel_facts_dao.list_facts("proj-1")
    assert facts[0]["fact_type"] == "completed"


# ── 查询分类 ────────────────────────────────────────────

def test_list_facts_returns_all_ordered(memory_db):
    novel_facts_dao.add_fact("proj-1", "completed", "事实A", "ch-1")
    novel_facts_dao.add_fact("proj-1", "revealed", "事实B", "ch-2")
    facts = novel_facts_dao.list_facts("proj-1")
    assert len(facts) == 2
    assert {f["content"] for f in facts} == {"事实A", "事实B"}


def test_list_completed_revealed_facts_excludes_state_changed(memory_db):
    novel_facts_dao.add_fact("proj-1", "completed", "完成契约", "ch-1")
    novel_facts_dao.add_fact("proj-1", "revealed", "身世揭晓", "ch-2")
    novel_facts_dao.add_fact("proj-1", "state_changed", "陈墨死亡", "ch-3")
    milestones = novel_facts_dao.list_completed_revealed_facts("proj-1")
    contents = {f["content"] for f in milestones}
    assert contents == {"完成契约", "身世揭晓"}


def test_list_recent_state_changed_facts_limits(memory_db):
    for index in range(5):
        novel_facts_dao.add_fact("proj-1", "state_changed", f"状态变化{index}", f"ch-{index}")
    recent = novel_facts_dao.list_recent_state_changed_facts("proj-1", limit=2)
    assert len(recent) == 2
    assert all(f["fact_type"] == "state_changed" for f in recent)


# ── 清理 ────────────────────────────────────────────────

def test_delete_facts_by_project(memory_db):
    novel_facts_dao.add_fact("proj-1", "completed", "事实A", "ch-1")
    novel_facts_dao.add_fact("proj-2", "completed", "事实B", "ch-1")
    novel_facts_dao.delete_facts_by_project("proj-1")
    assert novel_facts_dao.count_facts("proj-1") == 0
    assert novel_facts_dao.count_facts("proj-2") == 1


# ── 跨章矛盾消解：supersede_facts_by_ids（Codex P2 复测 P1） ──

@pytest.fixture
def memory_db_with_status(tmp_path):
    """带 status/version/valid_to/source_chapter_id 列的临时文件库（记忆升级 v1 结构）。

    用临时文件数据库，每次 get_conn() 返回新连接但共享同一数据文件，
    避免 DAO 方法 close() 影响其他调用。tmp_path 由 pytest 自动清理。
    """
    db_path = str(tmp_path / "novel_facts.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS novel_facts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            fact_type TEXT NOT NULL DEFAULT 'completed',
            content TEXT NOT NULL,
            chapter_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            is_conflict INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            entities_json TEXT NOT NULL DEFAULT '[]',
            relations_json TEXT NOT NULL DEFAULT '[]',
            source_chapter_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(project_id, content)
        );
        """
    )
    conn.commit()

    def _get_conn():
        # 每次返回新连接（共享同一数据文件），避免 DAO close() 影响其他调用
        new_conn = sqlite3.connect(db_path)
        new_conn.row_factory = sqlite3.Row
        return new_conn

    with patch.object(novel_facts_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


def test_supersede_facts_by_ids_marks_active_facts(memory_db_with_status):
    novel_facts_dao.add_fact("proj-1", "completed", "守碑人是陈九", "ch-1")
    novel_facts_dao.add_fact("proj-1", "completed", "沈墨取得木牌", "ch-1")
    facts = novel_facts_dao.list_facts("proj-1")
    target_id = next(f["id"] for f in facts if f["content"] == "守碑人是陈九")
    keep_id = next(f["id"] for f in facts if f["content"] == "沈墨取得木牌")

    count = novel_facts_dao.supersede_facts_by_ids("proj-1", [target_id])
    assert count == 1

    facts = novel_facts_dao.list_facts("proj-1")
    by_id = {f["id"]: f for f in facts}
    assert by_id[target_id]["status"] == "superseded"
    assert by_id[keep_id]["status"] == "active"


def test_supersede_facts_by_ids_ignores_other_project(memory_db_with_status):
    novel_facts_dao.add_fact("proj-1", "completed", "守碑人是陈九", "ch-1")
    novel_facts_dao.add_fact("proj-2", "completed", "守碑人是陈九", "ch-1")
    facts = novel_facts_dao.list_facts("proj-1")
    target_id = next(f["id"] for f in facts if f["content"] == "守碑人是陈九")

    # 用 proj-2 调用，不应影响 proj-1 的事实
    count = novel_facts_dao.supersede_facts_by_ids("proj-2", [target_id])
    assert count == 0
    facts = novel_facts_dao.list_facts("proj-1")
    assert all(f["status"] == "active" for f in facts)


def test_supersede_facts_by_ids_empty_list(memory_db_with_status):
    assert novel_facts_dao.supersede_facts_by_ids("proj-1", []) == 0
