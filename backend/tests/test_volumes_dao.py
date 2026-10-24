"""volumes（M1 卷级摘要）DAO 单元测试 —— 使用 SQLite 内存数据库。

覆盖：
- upsert_volume 插入 / 幂等更新（重复调用不报错）
- upsert 时已生成摘要不被空值覆盖
- get_volume / list_volumes / list_volume_summaries / update_volume_summary
- delete_volumes_by_project
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from app.dao import volumes as volumes_dao


@pytest.fixture
def memory_db():
    """独立 SQLite 内存数据库，建 volumes 表并 patch DAO 的 get_conn。

    表结构使用 (project_id, id) 复合唯一（P1-1 修复后 schema）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
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
        """
    )
    conn.commit()

    def _get_conn():
        return conn

    with patch.object(volumes_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


# ── upsert_volume ───────────────────────────────────────

def test_upsert_inserts_new_volume(memory_db):
    volumes_dao.upsert_volume(
        project_id="proj-1",
        volume_id="vol-1",
        title="第一卷",
        volume_order=1,
        start_chapter_id="c1",
        end_chapter_id="c10",
    )
    volume = volumes_dao.get_volume("proj-1", "vol-1")
    assert volume is not None
    assert volume["title"] == "第一卷"
    assert volume["order"] == 1
    assert volume["start_chapter_id"] == "c1"
    assert volume["end_chapter_id"] == "c10"


def test_upsert_is_idempotent(memory_db):
    """重复调用不报错，且 end_chapter_id 会被更新。"""
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1, end_chapter_id="c5")
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1, end_chapter_id="c10")
    volume = volumes_dao.get_volume("proj-1", "vol-1")
    assert volume["end_chapter_id"] == "c10"


def test_upsert_keeps_existing_summary(memory_db):
    """已生成卷摘要后再次 upsert（新摘要为空）不应覆盖已有摘要。"""
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1, summary="已生成的卷摘要")
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1)
    volume = volumes_dao.get_volume("proj-1", "vol-1")
    assert volume["summary"] == "已生成的卷摘要"


# ── 回归（P1-1）：跨项目同 id 卷互不覆盖 ─────────────

def test_same_volume_id_across_projects_do_not_overwrite(memory_db):
    """回归（P1-1）：不同项目可各自拥有同 id 卷（如 v1 / vol-1），互不覆盖。

    旧 schema 下 id 全局唯一，项目 B 对同 id 卷的 UPSERT 会整体覆盖项目 A 的记录；
    新 schema 用 UNIQUE(project_id, id) 后二者应各自独立。
    """
    volumes_dao.upsert_volume(
        "proj-a", "v1", title="项目A第一卷", volume_order=1, summary="A 的卷摘要",
    )
    volumes_dao.upsert_volume(
        "proj-b", "v1", title="项目B第一卷", volume_order=1, summary="B 的卷摘要",
    )

    vol_a = volumes_dao.get_volume("proj-a", "v1")
    vol_b = volumes_dao.get_volume("proj-b", "v1")

    assert vol_a is not None and vol_b is not None
    assert vol_a["summary"] == "A 的卷摘要"
    assert vol_b["summary"] == "B 的卷摘要"
    assert vol_a["title"] == "项目A第一卷"
    assert vol_b["title"] == "项目B第一卷"


def test_upsert_in_one_project_does_not_touch_other_projects_volume(memory_db):
    """回归（P1-1）：项目 B 再次 upsert 同 id 卷，项目 A 的记录保持不变。"""
    volumes_dao.upsert_volume("proj-a", "v1", title="项目A第一卷", volume_order=1, summary="A 的卷摘要")
    volumes_dao.upsert_volume("proj-b", "v1", title="项目B第一卷", volume_order=1, summary="B 的卷摘要")

    # 项目 B 更新自己的卷（空摘要不应覆盖），项目 A 应完全不受影响
    volumes_dao.upsert_volume("proj-b", "v1", title="项目B第一卷改", volume_order=1, end_chapter_id="c20")
    vol_a = volumes_dao.get_volume("proj-a", "v1")
    vol_b = volumes_dao.get_volume("proj-b", "v1")

    assert vol_a["summary"] == "A 的卷摘要"
    assert vol_a["title"] == "项目A第一卷"
    assert vol_a["end_chapter_id"] == ""
    assert vol_b["title"] == "项目B第一卷改"
    assert vol_b["end_chapter_id"] == "c20"
    assert vol_b["summary"] == "B 的卷摘要"


# ── 查询 ────────────────────────────────────────────────

def test_list_volumes_ordered_by_order(memory_db):
    volumes_dao.upsert_volume("proj-1", "vol-2", title="第二卷", volume_order=2)
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1)
    volumes = volumes_dao.list_volumes("proj-1")
    assert [v["title"] for v in volumes] == ["第一卷", "第二卷"]


def test_list_volume_summaries_only_non_empty(memory_db):
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1, summary="摘要一")
    volumes_dao.upsert_volume("proj-1", "vol-2", title="第二卷", volume_order=2, summary="")
    summaries = volumes_dao.list_volume_summaries("proj-1")
    assert summaries == ["摘要一"]


def test_update_volume_summary(memory_db):
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1)
    volumes_dao.update_volume_summary("proj-1", "vol-1", "新摘要")
    volume = volumes_dao.get_volume("proj-1", "vol-1")
    assert volume["summary"] == "新摘要"


# ── 清理 ────────────────────────────────────────────────

def test_delete_volumes_by_project(memory_db):
    volumes_dao.upsert_volume("proj-1", "vol-1", title="第一卷", volume_order=1)
    volumes_dao.upsert_volume("proj-2", "vol-1", title="第一卷", volume_order=1)
    volumes_dao.delete_volumes_by_project("proj-1")
    assert volumes_dao.list_volumes("proj-1") == []
    assert len(volumes_dao.list_volumes("proj-2")) == 1
