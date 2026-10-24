"""DAO 层单元测试 —— 使用 SQLite 内存数据库，不依赖真实数据文件。

覆盖：
- dao/projects.py  核心 CRUD（创建/查询/更新/删除/列表/存在检查/字数统计）
- dao/chapters.py  核心 CRUD（插入/查询/更新插入/删除/聚合字数）

设计原则：
- 每个测试用例独立（fixture 每次重建内存数据库）
- 覆盖正常路径 + 异常/边界路径
- 断言具体、可读
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from app.dao import projects as projects_dao
from app.dao import chapters as chapters_dao
from app.db import json_dump


# ══════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════

@pytest.fixture
def memory_db():
    """创建独立 SQLite 内存数据库，建表，并 patch 所有 DAO 模块的 get_conn。

    每个测试获得全新的数据库实例，互不干扰。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # ── 建表（与 app/db.py init_db() 中的 DDL 一致） ──
    conn.executescript("""
        CREATE TABLE chapter_trash (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_chapter_id TEXT NOT NULL,
            title TEXT NOT NULL, original_location TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL, removed_at TEXT NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
            chapter_json TEXT NOT NULL, commit_json TEXT NOT NULL DEFAULT '{}',
            derived_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            subtitle TEXT NOT NULL DEFAULT '',
            genre TEXT NOT NULL DEFAULT 'xuanhuan',
            target_word_count INTEGER NOT NULL DEFAULT 300000,
            current_word_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planning',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            style_config TEXT NOT NULL,
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

        CREATE TABLE IF NOT EXISTS chapters (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            volume_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            chapter_order INTEGER NOT NULL DEFAULT 1,
            content TEXT NOT NULL DEFAULT '',
            word_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            summary TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS volumes (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            start_chapter_id TEXT NOT NULL DEFAULT '',
            end_chapter_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            "order" INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, id)
        );
    """)
    conn.commit()

    # 让所有被测 DAO 模块的 get_conn 返回同一内存连接
    def _get_conn():
        return conn

    with patch.object(projects_dao, "get_conn", _get_conn), \
         patch.object(chapters_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


# ── 测试辅助：构造项目 fields ──────────────────────────

def _make_project_fields(
    project_id: str = "proj-001",
    name: str = "测试小说",
    genre: str = "xuanhuan",
    status: str = "planning",
    is_starred: int = 0,
    created_at: str = "2026-01-01",
    updated_at: str = "2026-01-01",
    **overrides,
) -> dict:
    """构造 projects 表插入所需的 fields 字典（值已序列化）。"""
    fields = {
        "id": project_id,
        "name": name,
        "subtitle": "测试副标题",
        "genre": genre,
        "target_word_count": 300000,
        "current_word_count": 0,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "style_config": json_dump({"name": "Epic", "avgSentenceLength": 28}),
        "outline": json_dump([{"title": "第一章大纲"}]),
        "tags": json_dump(["玄幻", "热血"]),
        "is_starred": is_starred,
        "premise": "一个宏大的世界观",
        "target_chapters": 30,
        "characters": json_dump([{"name": "主角"}]),
        "locations": json_dump([]),
        "world_rules": json_dump([]),
        "import_analysis": None,
        "inspiration_fragments": json_dump([]),
    }
    fields.update(overrides)
    return fields


def _make_chapter_fields(
    chapter_id: str = "ch-001",
    project_id: str = "proj-001",
    title: str = "第一章",
    chapter_order: int = 1,
    content: str = "这是第一章的内容。",
    word_count: int = 8,
    status: str = "draft",
    created_at: str = "2026-01-01",
    updated_at: str = "2026-01-01",
    **overrides,
) -> dict:
    """构造 chapters 表插入所需的 fields 字典（值已序列化）。"""
    fields = {
        "id": chapter_id,
        "project_id": project_id,
        "volume_id": "vol-1",
        "title": title,
        "chapter_order": chapter_order,
        "content": content,
        "word_count": word_count,
        "status": status,
        "summary": "本章摘要",
        "notes": "",
        "created_at": created_at,
        "updated_at": updated_at,
    }
    fields.update(overrides)
    return fields


# ══════════════════════════════════════════════════════
# projects DAO 测试
# ══════════════════════════════════════════════════════

class TestProjectsDAO:
    """projects 表数据访问层单元测试。"""

    # ── 创建 & 查询 ─────────────────────────────────

    def test_create_and_get_project(self, memory_db):
        """正常路径：创建项目后可通过 ID 查询到完整信息。"""
        projects_dao.create_project(_make_project_fields("proj-001", name="星辰变"))

        result = projects_dao.get_project("proj-001")

        assert result is not None
        assert result["id"] == "proj-001"
        assert result["name"] == "星辰变"
        assert result["genre"] == "xuanhuan"
        assert result["status"] == "planning"
        assert result["isStarred"] is False
        assert result["targetWordCount"] == 300000
        assert isinstance(result["styleConfig"], dict)
        assert isinstance(result["tags"], list)

    def test_get_project_not_found(self, memory_db):
        """异常路径：查询不存在的项目应返回 None。"""
        result = projects_dao.get_project("nonexistent-id")
        assert result is None

    def test_project_exists_true(self, memory_db):
        """边界条件：project_exists 对已存在项目返回 True。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        assert projects_dao.project_exists("proj-001") is True

    def test_project_exists_false(self, memory_db):
        """边界条件：project_exists 对不存在项目返回 False。"""
        assert projects_dao.project_exists("no-such-project") is False

    # ── 列表 & 排序 ─────────────────────────────────

    def test_list_projects_empty(self, memory_db):
        """边界条件：空数据库下列表返回空列表。"""
        result = projects_dao.list_projects()
        assert result == []

    def test_list_projects_ordering(self, memory_db):
        """正常路径：列表按 is_starred DESC, updated_at DESC 排序。"""
        # 插入三个项目：一个收藏的、两个非收藏的（不同更新时间）
        projects_dao.create_project(_make_project_fields(
            "proj-a", name="A", is_starred=0,
            created_at="2026-01-01", updated_at="2026-01-02",
        ))
        projects_dao.create_project(_make_project_fields(
            "proj-b", name="B", is_starred=1,
            created_at="2026-01-01", updated_at="2026-01-01",
        ))
        projects_dao.create_project(_make_project_fields(
            "proj-c", name="C", is_starred=0,
            created_at="2026-01-01", updated_at="2026-01-03",
        ))

        result = projects_dao.list_projects()

        # B(starred) > C(updated later) > A(updated earlier)
        assert [p["id"] for p in result] == ["proj-b", "proj-c", "proj-a"]

    # ── 更新 ───────────────────────────────────────

    def test_update_project_fields(self, memory_db):
        """正常路径：更新部分字段后查询应反映变更。"""
        projects_dao.create_project(_make_project_fields("proj-001", name="旧名称"))

        rows = projects_dao.update_project_fields(
            "proj-001",
            {"name": "新名称", "status": "writing"},
            updated_at="2026-06-01",
        )

        assert rows == 1
        updated = projects_dao.get_project("proj-001")
        assert updated["name"] == "新名称"
        assert updated["status"] == "writing"
        assert updated["updatedAt"] == "2026-06-01"

    def test_update_project_fields_no_rows(self, memory_db):
        """边界条件：更新不存在的项目应返回 0。"""
        rows = projects_dao.update_project_fields(
            "ghost-id",
            {"name": "不存在"},
            updated_at="2026-06-01",
        )
        assert rows == 0

    def test_update_project_word_count_and_status(self, memory_db):
        """正常路径：直接设置字数和状态。"""
        projects_dao.create_project(_make_project_fields("proj-001"))

        projects_dao.update_project_word_count_and_status(
            "proj-001", total_words=50000, status="writing", today="2026-06-01",
        )

        updated = projects_dao.get_project("proj-001")
        assert updated["currentWordCount"] == 50000
        assert updated["status"] == "writing"

    # ── 删除 ───────────────────────────────────────

    def test_delete_project(self, memory_db):
        """正常路径：删除后项目不可查询。"""
        projects_dao.create_project(_make_project_fields("proj-001"))

        rows = projects_dao.delete_project("proj-001")

        assert rows == 1
        assert projects_dao.get_project("proj-001") is None

    def test_delete_project_no_rows(self, memory_db):
        """边界条件：删除不存在的项目应返回 0。"""
        rows = projects_dao.delete_project("nonexistent")
        assert rows == 0


# ══════════════════════════════════════════════════════
# chapters DAO 测试
# ══════════════════════════════════════════════════════

class TestChaptersDAO:
    """chapters 表数据访问层单元测试。"""

    # ── 插入 & 列表 ─────────────────────────────────

    def test_insert_and_list_chapters(self, memory_db):
        """正常路径：插入章节后可按项目列出，并按 chapter_order 升序排列。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-02", "proj-001", title="第二章", chapter_order=2,
        ))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-01", "proj-001", title="第一章", chapter_order=1,
        ))

        result = chapters_dao.list_chapters("proj-001")

        assert len(result) == 2
        assert result[0]["order"] == 1
        assert result[0]["title"] == "第一章"
        assert result[1]["order"] == 2
        assert result[1]["title"] == "第二章"

    def test_list_chapters_empty_project(self, memory_db):
        """边界条件：无章节的项目返回空列表。"""
        projects_dao.create_project(_make_project_fields("proj-001"))

        result = chapters_dao.list_chapters("proj-001")

        assert result == []

    def test_list_chapters_global_order_multivolume(self, memory_db):
        """Codex P2 复测 P1：多卷章节必须按「卷 order + 卷内 chapter_order」全局排序。

        旧实现 `list_chapters` 只按 chapter_order 排序，chapter_order 每卷从 1
        重新开始，会把「第一卷 第1章、第二卷 第1章……」交错排列。
        `list_chapters_global_order` 必须返回：v1c1, v1c2, v2c1, v2c2。
        """
        projects_dao.create_project(_make_project_fields("proj-001"))
        # 建卷（order 决定卷顺序）
        with memory_db:
            memory_db.execute(
                "INSERT INTO volumes (id, project_id, title, \"order\", created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("vol-1", "proj-001", "第一卷", 1, "2026-01-01", "2026-01-01"),
            )
            memory_db.execute(
                "INSERT INTO volumes (id, project_id, title, \"order\", created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("vol-2", "proj-001", "第二卷", 2, "2026-01-01", "2026-01-01"),
            )
        # 插入章节（故意乱序插入，验证排序）
        chapters_dao.insert_chapter(_make_chapter_fields(
            "v2c1", "proj-001", title="第二卷 第1章", chapter_order=1, volume_id="vol-2",
        ))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "v1c2", "proj-001", title="第一卷 第2章", chapter_order=2, volume_id="vol-1",
        ))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "v1c1", "proj-001", title="第一卷 第1章", chapter_order=1, volume_id="vol-1",
        ))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "v2c2", "proj-001", title="第二卷 第2章", chapter_order=2, volume_id="vol-2",
        ))

        result = chapters_dao.list_chapters_global_order("proj-001")

        assert [c["id"] for c in result] == ["v1c1", "v1c2", "v2c1", "v2c2"]
        # 对照：旧 list_chapters 只按 chapter_order 排（同 order 按 created_at），
        # 会交错排列（v2c1, v1c1, v1c2, v2c2）——证明旧实现多卷顺序错误
        old = chapters_dao.list_chapters("proj-001")
        assert [c["id"] for c in old] == ["v2c1", "v1c1", "v1c2", "v2c2"]

    def test_list_chapters_global_order_no_volume_last(self, memory_db):
        """无卷归属的章节排在最后（Codex P2 复测 P1 边界）。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        with memory_db:
            memory_db.execute(
                "INSERT INTO volumes (id, project_id, title, \"order\", created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("vol-1", "proj-001", "第一卷", 1, "2026-01-01", "2026-01-01"),
            )
        chapters_dao.insert_chapter(_make_chapter_fields(
            "v1c1", "proj-001", title="第一卷 第1章", chapter_order=1, volume_id="vol-1",
        ))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "draft1", "proj-001", title="无卷草稿", chapter_order=1, volume_id="",
        ))

        result = chapters_dao.list_chapters_global_order("proj-001")

        assert [c["id"] for c in result] == ["v1c1", "draft1"]

    # ── 查询 ───────────────────────────────────────

    def test_get_chapter_found(self, memory_db):
        """正常路径：通过 chapter_id + project_id 查询到章节详情。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-001", "proj-001", title="觉醒", chapter_order=1,
            content="天地初开……", word_count=500, status="completed",
        ))

        result = chapters_dao.get_chapter("ch-001", "proj-001")

        assert result is not None
        assert result["id"] == "ch-001"
        assert result["projectId"] == "proj-001"
        assert result["title"] == "觉醒"
        assert result["order"] == 1
        assert result["wordCount"] == 500
        assert result["status"] == "completed"

    def test_get_chapter_not_found(self, memory_db):
        """异常路径：查询不存在的章节返回 None。"""
        assert chapters_dao.get_chapter("ghost-ch", "proj-001") is None

    def test_get_chapter_content(self, memory_db):
        """正常路径：get_chapter_content 只返回正文文本。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-001", "proj-001", content="这是一段正文内容，用于测试。",
        ))

        content = chapters_dao.get_chapter_content("proj-001", "ch-001")

        assert "正文内容" in content
        assert "测试" in content

    def test_get_chapter_content_empty(self, memory_db):
        """边界条件：章节无内容时返回空字符串。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-001", "proj-001", content="",
        ))

        content = chapters_dao.get_chapter_content("proj-001", "ch-001")

        assert content == ""

    # ── Upsert（插入或更新） ────────────────────────

    def test_upsert_chapter_insert(self, memory_db):
        """正常路径：upsert 不存在的章节 → INSERT 新记录。"""
        projects_dao.create_project(_make_project_fields("proj-001"))

        result = chapters_dao.upsert_chapter(
            "ch-new", "proj-001",
            fields={"title": "新章节", "content": "新内容", "word_count": 100},
            today="2026-06-01",
        )

        assert result is not None
        assert result["id"] == "ch-new"
        assert result["title"] == "新章节"
        assert result["wordCount"] == 100
        assert result["createdAt"] == "2026-06-01"

    def test_upsert_chapter_update(self, memory_db):
        """正常路径：upsert 已存在的章节 → UPDATE 已有记录。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-001", "proj-001", title="旧标题", word_count=50,
            created_at="2026-01-01", updated_at="2026-01-01",
        ))

        result = chapters_dao.upsert_chapter(
            "ch-001", "proj-001",
            fields={"title": "新标题", "content": "更新后的内容", "word_count": 200},
            today="2026-06-15",
        )

        assert result["title"] == "新标题"
        assert result["wordCount"] == 200
        # created_at 应保留原值，updated_at 应更新
        assert result["createdAt"] == "2026-01-01"
        assert result["updatedAt"] == "2026-06-15"

    # ── 删除 ───────────────────────────────────────

    def test_delete_chapter(self, memory_db):
        """正常路径：删除章节后不可查询。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields("ch-001", "proj-001"))

        rows = chapters_dao.delete_chapter("ch-001", "proj-001")

        assert rows == 1
        assert chapters_dao.get_chapter("ch-001", "proj-001") is None

    def test_delete_chapter_no_rows(self, memory_db):
        """边界条件：删除不存在的章节返回 0。"""
        rows = chapters_dao.delete_chapter("ghost", "proj-001")
        assert rows == 0

    def test_delete_chapters_by_project(self, memory_db):
        """正常路径：按项目批量删除所有章节。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields("ch-001", "proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields("ch-002", "proj-001", chapter_order=2))

        chapters_dao.delete_chapters_by_project("proj-001")

        assert chapters_dao.list_chapters("proj-001") == []

    # ── 聚合 ───────────────────────────────────────

    def test_aggregate_word_count(self, memory_db):
        """正常路径：aggregate_word_count 正确汇总项目下所有章节字数。"""
        projects_dao.create_project(_make_project_fields("proj-001"))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-001", "proj-001", word_count=1500,
        ))
        chapters_dao.insert_chapter(_make_chapter_fields(
            "ch-002", "proj-001", chapter_order=2, word_count=2500,
        ))

        total = chapters_dao.aggregate_word_count("proj-001")

        assert total == 4000

    def test_aggregate_word_count_empty(self, memory_db):
        """边界条件：项目无章节时返回 0。"""
        projects_dao.create_project(_make_project_fields("proj-001"))

        total = chapters_dao.aggregate_word_count("proj-001")

        assert total == 0

    def test_aggregate_word_count_nonexistent_project(self, memory_db):
        """边界条件：不存在的项目返回 0。"""
        total = chapters_dao.aggregate_word_count("no-project")
        assert total == 0
