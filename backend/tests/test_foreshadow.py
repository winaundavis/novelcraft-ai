"""P2 伏笔账本（长程跨卷张力）—— foreshadow_service / foreshadows DAO / API 单测。

覆盖：
- DAO：建/查/改/删、状态机正向迁移（planted→progressing→resolved/abandoned）、
  非法回退拒绝、事件追加、同状态更新幂等（不重复记事件）、项目间数据隔离；
- 服务：suggest_foreshadows（LLM mock 返回候选 / 代码块围栏容错 / LLM 异常降级为空、
  缺大纲/缺项目返回空、越界章裁剪）、确认入库、活跃伏笔筛选（只取 planted/progressing，
  未到埋设章不注入）、build_foreshadow_instruction（注入 + 超期告警）、
  update_foreshadows_after_chapter（resolved/abandoned/事件记录 / LLM 异常降级不阻塞 /
  非法 id 与回退状态忽略）、count_overdue_foreshadows（target_chapter+3 阈值）、
  check_foreshadow_outline_consistency（五类问题）、enrich_foreshadows（富化字段）；
- API：五端点路由可达 + 404（项目不存在 / 伏笔不存在）+ 400（空批量）。

所有 LLM 调用均 AsyncMock 打桩（秒级完成）；数据库用独立内存 SQLite 隔离。
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.dao import foreshadows as foreshadows_dao
from app.dao import projects as projects_dao
from app.dao.foreshadows import (
    FORESHADOW_STATUS_ABANDONED,
    FORESHADOW_STATUS_PLANTED,
    FORESHADOW_STATUS_PROGRESSING,
    FORESHADOW_STATUS_RESOLVED,
)
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.services import foreshadow_service
from app.services.foreshadow_service import (
    build_foreshadow_instruction,
    check_foreshadow_outline_consistency,
    count_overdue_foreshadows,
    enrich_foreshadows,
    suggest_foreshadows,
    update_foreshadows_after_chapter,
)
from main import app


# ── 内存库 Schema ──────────────────────────────────────

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

FORESHADOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS foreshadows (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    plant_chapter_id TEXT NOT NULL DEFAULT '',
    target_chapter_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planted',
    resolution TEXT NOT NULL DEFAULT '',
    events_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _build_memory_db() -> sqlite3.Connection:
    # check_same_thread=False：TestClient 在独立线程中处理请求，需允许跨线程访问
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(PROJECTS_SCHEMA + FORESHADOWS_SCHEMA)
    conn.commit()
    return conn


def _get_conn_factory(conn: sqlite3.Connection):
    def _get_conn() -> sqlite3.Connection:
        return conn
    return _get_conn


# ── 通用 Fixtures ─────────────────────────────────────

@pytest.fixture
def foreshadow_db():
    """DAO 级隔离库：仅 foreshadows 表 + patch foreshadows_dao.get_conn。"""
    conn = _build_memory_db()

    def _get_conn():
        return conn

    with patch.object(foreshadows_dao, "get_conn", _get_conn):
        yield conn
    conn.close()


@pytest.fixture
def env():
    """服务级隔离库：projects + foreshadows 表，patch 两个 DAO 的 get_conn。"""
    conn = _build_memory_db()
    get_conn = _get_conn_factory(conn)
    with patch.object(projects_dao, "get_conn", get_conn), patch.object(
        foreshadows_dao, "get_conn", get_conn
    ):
        yield conn
    conn.close()


@pytest.fixture
def api_env():
    """API 级隔离库：patch 两个 DAO 的 get_conn（TestClient 处理请求时生效）。"""
    conn = _build_memory_db()
    get_conn = _get_conn_factory(conn)
    with patch.object(projects_dao, "get_conn", get_conn), patch.object(
        foreshadows_dao, "get_conn", get_conn
    ):
        yield conn
    conn.close()


# ── 测试数据工具 ──────────────────────────────────────

PROJECT_ID = "proj-test-1"
OTHER_PROJECT_ID = "proj-test-2"


def _outline() -> dict:
    """10 章大纲（章节 id 与序号对应：ch1..ch10）。"""
    return {
        "mainSummary": "测试用全书大纲：主线为少年寻剑。",
        "nodes": [
            {
                "id": f"ch{i}",
                "type": "chapter",
                "title": f"第{i}章",
                "order": i,
                "summary": f"第{i}章摘要",
            }
            for i in range(1, 11)
        ],
    }


def _insert_project(
    conn: sqlite3.Connection,
    project_id: str = PROJECT_ID,
    outline: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO projects
            (id, name, genre, status, created_at, updated_at, style_config, outline, target_chapters)
        VALUES (?, '测试项目', 'xuanhuan', 'planning', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z', '{}', ?, 10)
        """,
        (project_id, json.dumps(outline, ensure_ascii=False) if outline else None),
    )
    conn.commit()


def _create_foreshadow(
    conn: sqlite3.Connection,
    project_id: str = PROJECT_ID,
    *,
    name: str = "剑的秘密",
    description: str = "主角佩剑暗藏上一代剑主的遗志",
    plant_chapter_id: str = "ch1",
    target_chapter_id: str = "ch8",
    status: str = FORESHADOW_STATUS_PLANTED,
    resolution: str = "",
    events_json: str = "[]",
) -> str:
    fid = f"fid-{name}-{status}"
    conn.execute(
        """
        INSERT INTO foreshadows
            (id, project_id, name, description, plant_chapter_id, target_chapter_id,
             status, resolution, events_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')
        """,
        (
            fid, project_id, name, description,
            plant_chapter_id, target_chapter_id,
            status, resolution, events_json,
        ),
    )
    conn.commit()
    return fid


# ═══════════════════════════════════════════════════════════
# DAO：建 / 查 / 改 / 删 / 状态迁移 / 幂等
# ═══════════════════════════════════════════════════════════

class TestForeshadowDaoCreate:
    def test_create_and_get(self, foreshadow_db):
        fid = foreshadows_dao.create_foreshadow(
            PROJECT_ID,
            name="青铜钥匙",
            description="密室钥匙的来历",
            plant_chapter_id="ch2",
            target_chapter_id="ch15",
        )
        item = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)
        assert item is not None
        assert item["id"] == fid
        assert item["project_id"] == PROJECT_ID
        assert item["name"] == "青铜钥匙"
        assert item["description"] == "密室钥匙的来历"
        assert item["plant_chapter_id"] == "ch2"
        assert item["target_chapter_id"] == "ch15"
        assert item["status"] == FORESHADOW_STATUS_PLANTED
        assert item["resolution"] == ""
        assert item["events"] == []

    def test_create_defaults_empty_name_and_invalid_status(self, foreshadow_db):
        fid = foreshadows_dao.create_foreshadow(PROJECT_ID, name="   ")
        item = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)
        assert item["name"] == "未命名伏笔"
        assert item["status"] == FORESHADOW_STATUS_PLANTED

        fid2 = foreshadows_dao.create_foreshadow(PROJECT_ID, name="x", status="weird")
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid2)["status"] == FORESHADOW_STATUS_PLANTED

    def test_create_batch_and_isolates_projects(self, foreshadow_db):
        items = [
            {"name": "A", "description": "dA", "plant_chapter_id": "ch1", "target_chapter_id": "ch9"},
            {"name": "B", "description": "dB", "plant_chapter_id": "ch3", "target_chapter_id": "ch20"},
        ]
        ids = foreshadows_dao.create_foreshadows_batch(PROJECT_ID, items)
        assert len(ids) == 2
        assert foreshadows_dao.count_foreshadows(PROJECT_ID) == 2

        # 项目间数据隔离：另一项目看不到本项目伏笔
        assert foreshadows_dao.count_foreshadows(OTHER_PROJECT_ID) == 0
        assert foreshadows_dao.list_foreshadows(OTHER_PROJECT_ID) == []
        assert foreshadows_dao.get_foreshadow(OTHER_PROJECT_ID, ids[0]) is None


class TestForeshadowDaoQuery:
    def test_list_and_count(self, foreshadow_db):
        _create_foreshadow(foreshadow_db, name="伏笔1")
        _create_foreshadow(foreshadow_db, name="伏笔2")
        assert len(foreshadows_dao.list_foreshadows(PROJECT_ID)) == 2
        assert foreshadows_dao.count_foreshadows(PROJECT_ID) == 2

    def test_list_active_only_returns_planted_progressing(self, foreshadow_db):
        _create_foreshadow(foreshadow_db, name="埋设", status=FORESHADOW_STATUS_PLANTED)
        _create_foreshadow(foreshadow_db, name="推进", status=FORESHADOW_STATUS_PROGRESSING)
        _create_foreshadow(foreshadow_db, name="已回收", status=FORESHADOW_STATUS_RESOLVED)
        _create_foreshadow(foreshadow_db, name="已放弃", status=FORESHADOW_STATUS_ABANDONED)

        active = foreshadows_dao.list_active_foreshadows(PROJECT_ID)
        names = {item["name"] for item in active}
        assert names == {"埋设", "推进"}
        assert foreshadows_dao.count_active_foreshadows(PROJECT_ID) == 2

    def test_list_active_limit(self, foreshadow_db):
        for index in range(5):
            _create_foreshadow(foreshadow_db, name=f"伏笔{index}", status=FORESHADOW_STATUS_PLANTED)
        assert len(foreshadows_dao.list_active_foreshadows(PROJECT_ID, limit=2)) == 2


class TestForeshadowDaoUpdate:
    def test_update_fields(self, foreshadow_db):
        fid = _create_foreshadow(foreshadow_db)
        ok = foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid, name="改名后的伏笔", description="新描述"
        )
        assert ok is True
        item = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)
        assert item["name"] == "改名后的伏笔"
        assert item["description"] == "新描述"

    def test_update_forward_status_migration(self, foreshadow_db):
        fid = _create_foreshadow(foreshadow_db, status=FORESHADOW_STATUS_PLANTED)
        # planted → progressing
        assert foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid, status=FORESHADOW_STATUS_PROGRESSING, event_note="本章出现暗示"
        ) is True
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["status"] == FORESHADOW_STATUS_PROGRESSING
        # progressing → resolved（带回收方式）
        assert foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid, status=FORESHADOW_STATUS_RESOLVED, resolution="主角认出剑柄刻痕", event_note="揭晓"
        ) is True
        item = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)
        assert item["status"] == FORESHADOW_STATUS_RESOLVED
        assert item["resolution"] == "主角认出剑柄刻痕"
        # planted → abandoned 也合法
        fid2 = _create_foreshadow(foreshadow_db, name="放弃线", status=FORESHADOW_STATUS_PLANTED)
        assert foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid2, status=FORESHADOW_STATUS_ABANDONED
        ) is True
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid2)["status"] == FORESHADOW_STATUS_ABANDONED

    def test_update_illegal_status_migration_rejected(self, foreshadow_db):
        fid = _create_foreshadow(foreshadow_db, name="回退线", status=FORESHADOW_STATUS_RESOLVED)
        # resolved → planted 回退被拒
        assert foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid, status=FORESHADOW_STATUS_PLANTED
        ) is False
        # progressing → planted 回退被拒
        fid2 = _create_foreshadow(foreshadow_db, name="推进线", status=FORESHADOW_STATUS_PROGRESSING)
        assert foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid2, status=FORESHADOW_STATUS_PLANTED
        ) is False
        # 状态未变（幂等）
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["status"] == FORESHADOW_STATUS_RESOLVED
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid2)["status"] == FORESHADOW_STATUS_PROGRESSING

    def test_update_appends_event(self, foreshadow_db):
        fid = _create_foreshadow(foreshadow_db)
        foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid,
            status=FORESHADOW_STATUS_PROGRESSING,
            event_note="密室线索出现",
            chapter_id="ch3",
            chapter_order=3,
        )
        item = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)
        assert len(item["events"]) == 1
        event = item["events"][0]
        assert event["chapter_id"] == "ch3"
        assert event["chapter_order"] == 3
        assert event["status"] == FORESHADOW_STATUS_PROGRESSING
        assert event["note"] == "密室线索出现"

    def test_update_idempotent_same_status_no_duplicate_event(self, foreshadow_db):
        fid = _create_foreshadow(foreshadow_db, status=FORESHADOW_STATUS_PROGRESSING)
        foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid, status=FORESHADOW_STATUS_PROGRESSING, event_note="第一次推进"
        )
        assert len(foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["events"]) == 1
        # 相同状态 + 无事件说明 → 幂等，不追加事件
        foreshadows_dao.update_foreshadow(PROJECT_ID, fid, status=FORESHADOW_STATUS_PROGRESSING)
        assert len(foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["events"]) == 1

    def test_update_nonexistent_returns_false(self, foreshadow_db):
        assert foreshadows_dao.update_foreshadow(PROJECT_ID, "not-exist", name="x") is False
        # 跨项目更新不存在（项目隔离）
        _create_foreshadow(foreshadow_db)
        assert foreshadows_dao.update_foreshadow(OTHER_PROJECT_ID, "fid-剑的秘密-planted", name="x") is False


class TestForeshadowDaoDelete:
    def test_delete(self, foreshadow_db):
        fid = _create_foreshadow(foreshadow_db)
        assert foreshadows_dao.delete_foreshadow(PROJECT_ID, fid) is True
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid) is None
        assert foreshadows_dao.delete_foreshadow(PROJECT_ID, fid) is False

    def test_delete_by_project(self, foreshadow_db):
        _create_foreshadow(foreshadow_db, name="A")
        _create_foreshadow(foreshadow_db, name="B")
        foreshadows_dao.delete_foreshadows_by_project(PROJECT_ID)
        assert foreshadows_dao.count_foreshadows(PROJECT_ID) == 0


# ═══════════════════════════════════════════════════════════
# 服务：建议伏笔（suggest_foreshadows）
# ═══════════════════════════════════════════════════════════

class TestSuggestForeshadows:
    @pytest.mark.asyncio
    async def test_suggest_with_llm_candidates(self, env):
        _insert_project(env, outline=_outline())
        reply = json.dumps([
            {"name": "剑的秘密", "description": "主角佩剑暗藏上一代剑主的遗志", "plant_chapter": 1, "target_chapter": 8},
            {"name": "失踪的信使", "description": "送信人从未抵达的真相", "plant_chapter": 3, "target_chapter": 25},
        ], ensure_ascii=False)
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ):
            candidates = await suggest_foreshadows(PROJECT_ID)
        assert len(candidates) == 2
        first = candidates[0]
        assert first["name"] == "剑的秘密"
        assert first["plant_chapter"] == 1
        assert first["target_chapter"] == 8
        assert first["plant_chapter_id"] == "ch1"
        assert first["target_chapter_id"] == "ch8"
        # 越界章裁剪：target 25 > 10 → 10
        second = candidates[1]
        assert second["target_chapter"] == 10
        assert second["target_chapter_id"] == "ch10"

    @pytest.mark.asyncio
    async def test_suggest_handles_fenced_and_garbage(self, env):
        _insert_project(env, outline=_outline())
        reply = (
            "好的，以下是建议：\n"
            "```json\n"
            '[{"name": "回响", "description": "旧城堡里的回声来自过去的自己", "plant_chapter": 2, "target_chapter": 9}]\n'
            "```\n"
            "希望对你有帮助。"
        )
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ):
            candidates = await suggest_foreshadows(PROJECT_ID)
        assert len(candidates) == 1
        assert candidates[0]["name"] == "回响"

    @pytest.mark.asyncio
    async def test_suggest_llm_error_degrades_empty(self, env):
        _insert_project(env, outline=_outline())
        with patch.object(
            foreshadow_service.llm, "chat",
            new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
        ):
            assert await suggest_foreshadows(PROJECT_ID) == []
        with patch.object(
            foreshadow_service.llm, "chat",
            new=AsyncMock(side_effect=LLMResponseError("上游失败")),
        ):
            assert await suggest_foreshadows(PROJECT_ID) == []

    @pytest.mark.asyncio
    async def test_suggest_missing_outline_returns_empty(self, env):
        _insert_project(env, outline=None)
        with patch.object(foreshadow_service.llm, "chat", new=AsyncMock()) as mock:
            assert await suggest_foreshadows(PROJECT_ID) == []
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suggest_missing_project_returns_empty(self, env):
        with patch.object(foreshadow_service.llm, "chat", new=AsyncMock()) as mock:
            assert await suggest_foreshadows(PROJECT_ID) == []
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suggest_invalid_reply_degrades_empty(self, env):
        _insert_project(env, outline=_outline())
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value="完全没有JSON的回复")
        ):
            assert await suggest_foreshadows(PROJECT_ID) == []


# ═══════════════════════════════════════════════════════════
# 服务：确认入库 + 活跃伏笔筛选
# ═══════════════════════════════════════════════════════════

class TestConfirmAndActive:
    def test_confirm_batch_inserts_planted(self, env):
        _insert_project(env, outline=_outline())
        ids = foreshadows_dao.create_foreshadows_batch(
            PROJECT_ID,
            [
                {"name": "A", "description": "dA", "plant_chapter_id": "ch1", "target_chapter_id": "ch7"},
                {"name": "B", "description": "dB", "plant_chapter_id": "ch2", "target_chapter_id": "ch9"},
            ],
        )
        assert len(ids) == 2
        for fid in ids:
            assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["status"] == FORESHADOW_STATUS_PLANTED

    def test_build_instruction_injects_active_only(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="剑的秘密", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        _create_foreshadow(env, name="推进线", status=FORESHADOW_STATUS_PROGRESSING, plant_chapter_id="ch2", target_chapter_id="ch6")
        _create_foreshadow(env, name="已回收", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch4")
        _create_foreshadow(env, name="未到埋设章", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch5", target_chapter_id="ch9")

        instruction = build_foreshadow_instruction(PROJECT_ID, _outline(), current_chapter_id="ch3")
        assert "剑的秘密" in instruction
        assert "推进线" in instruction
        assert "已回收" not in instruction
        assert "未到埋设章" not in instruction  # 未到埋设章（ch5 > ch3）不注入
        assert "第8章" in instruction
        assert "已埋设" in instruction
        assert "推进中" in instruction

    def test_build_instruction_empty_when_no_active(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="已回收", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch4")
        assert build_foreshadow_instruction(PROJECT_ID, _outline(), current_chapter_id="ch3") == ""

    def test_build_instruction_includes_overdue_warning(self, env):
        _insert_project(env, outline=_outline())
        # target=ch5，当前 ch9 → 9 > 5+3 超期
        _create_foreshadow(env, name="超期线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch5")
        instruction = build_foreshadow_instruction(PROJECT_ID, _outline(), current_chapter_id="ch9")
        assert "伏笔超期告警" in instruction
        assert "超期线" in instruction

    def test_count_overdue_threshold(self, env):
        _insert_project(env, outline=_outline())
        # target=ch5；当前 ch8（=5+3）不算超期，ch9（>5+3）算超期
        _create_foreshadow(env, name="边界线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch5")
        assert count_overdue_foreshadows(PROJECT_ID, _outline(), current_chapter_id="ch8") == 0
        assert count_overdue_foreshadows(PROJECT_ID, _outline(), current_chapter_id="ch9") == 1
        # 已回收的不计超期
        _create_foreshadow(env, name="回收线", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch2")
        assert count_overdue_foreshadows(PROJECT_ID, _outline(), current_chapter_id="ch9") == 1


# ═══════════════════════════════════════════════════════════
# 服务：超期明细（按章序号，供整书自动创作注入）
# ═══════════════════════════════════════════════════════════

class TestOverdueActiveForeshadows:
    def test_grace_boundary_by_order(self, env):
        _insert_project(env, outline=_outline())
        # target=ch5；第 8 章（=5+3）不算超期，第 9 章算超期
        _create_foreshadow(env, name="边界线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch5")
        assert foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 8) == []
        assert foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 9) == [
            {"name": "边界线", "target_order": 5, "overdue_by": 4}
        ]

    def test_skips_resolved_and_unplanted_and_empty_order(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="已回收", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch2")
        _create_foreshadow(env, name="未到埋设章", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch9", target_chapter_id="ch10")
        assert foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 9) == []
        assert foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 0) == []

    def test_sorted_by_overdue_desc_and_capped(self, env):
        _insert_project(env, outline=_outline())
        # 轻超期：target=ch6 → 10 > 6+3，超期 4 章；重超期：target=ch2 → 超期 8 章
        _create_foreshadow(env, name="轻超期", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch6")
        _create_foreshadow(env, name="重超期", status=FORESHADOW_STATUS_PROGRESSING, plant_chapter_id="ch1", target_chapter_id="ch2")
        overdue = foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 10)
        assert [entry["name"] for entry in overdue] == ["重超期", "轻超期"]
        assert overdue[0]["overdue_by"] == 8
        assert overdue[1]["overdue_by"] == 4
        assert len(foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 10, limit=1)) == 1

    def test_unexpected_error_returns_empty(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch2")
        with patch.object(
            foreshadows_dao, "list_active_foreshadows", side_effect=RuntimeError("库炸了")
        ):
            assert foreshadow_service.overdue_active_foreshadows(PROJECT_ID, 9) == []


# ═══════════════════════════════════════════════════════════
# 服务：整书自动创作的「未回收伏笔」注入块（超期标注）
# ═══════════════════════════════════════════════════════════

class TestActiveForeshadowInjectionBlock:
    def test_marks_overdue_when_current_order_known(self, env):
        from app.services import novel_generation_service as ngs

        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="超期线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch3")
        block = ngs._build_active_foreshadows(PROJECT_ID, current_order=9)
        assert "未回收伏笔" in block
        assert "已超期 6 章" in block
        assert "预计第 3 章回收" in block
        assert "应优先给出推进或回收线索" in block

    def test_no_overdue_marker_without_current_order(self, env):
        from app.services import novel_generation_service as ngs

        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="超期线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch3")
        block = ngs._build_active_foreshadows(PROJECT_ID)
        assert "超期线" in block
        assert "已超期" not in block  # 无当前章序号时不做超期判定（零多余查询）


# ═══════════════════════════════════════════════════════════
# 服务：自动追踪（update_foreshadows_after_chapter）
# ═══════════════════════════════════════════════════════════

class TestUpdateForeshadowsAfterChapter:
    @pytest.mark.asyncio
    async def test_update_resolved_and_abandoned_with_events(self, env):
        _insert_project(env, outline=_outline())
        fid1 = _create_foreshadow(env, name="回收线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch6")
        fid2 = _create_foreshadow(env, name="放弃线", status=FORESHADOW_STATUS_PROGRESSING, plant_chapter_id="ch2", target_chapter_id="ch7")

        reply = json.dumps({
            "updates": [
                {"id": fid1, "status": "resolved", "event": "本章揭晓剑鞘刻痕的来历", "resolution": "主角认出刻痕为前任剑主所留"},
                {"id": fid2, "status": "abandoned", "event": "作者决定砍掉这条支线"},
            ]
        }, ensure_ascii=False)
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ):
            updated = await update_foreshadows_after_chapter(
                PROJECT_ID, "ch6", 6, "本章正文内容……" * 50, _outline()
            )
        assert updated == 2
        item1 = foreshadows_dao.get_foreshadow(PROJECT_ID, fid1)
        assert item1["status"] == FORESHADOW_STATUS_RESOLVED
        assert item1["resolution"] == "主角认出刻痕为前任剑主所留"
        assert len(item1["events"]) == 1
        assert item1["events"][0]["chapter_order"] == 6
        assert item1["events"][0]["note"] == "本章揭晓剑鞘刻痕的来历"
        item2 = foreshadows_dao.get_foreshadow(PROJECT_ID, fid2)
        assert item2["status"] == FORESHADOW_STATUS_ABANDONED
        assert len(item2["events"]) == 1

    @pytest.mark.asyncio
    async def test_update_progresses_planted(self, env):
        _insert_project(env, outline=_outline())
        fid = _create_foreshadow(env, name="推进线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        reply = json.dumps({
            "updates": [{"id": fid, "status": "progressing", "event": "本章出现新线索"}]
        }, ensure_ascii=False)
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ):
            updated = await update_foreshadows_after_chapter(PROJECT_ID, "ch3", 3, "正文" * 100, _outline())
        assert updated == 1
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["status"] == FORESHADOW_STATUS_PROGRESSING

    @pytest.mark.asyncio
    async def test_update_llm_error_degrades_zero(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        with patch.object(
            foreshadow_service.llm, "chat",
            new=AsyncMock(side_effect=LLMResponseError("上游失败")),
        ):
            updated = await update_foreshadows_after_chapter(PROJECT_ID, "ch3", 3, "正文" * 100, _outline())
        assert updated == 0  # 降级跳过不阻塞

    @pytest.mark.asyncio
    async def test_update_ignores_unknown_id_and_illegal_status(self, env):
        _insert_project(env, outline=_outline())
        # 初始为 progressing；LLM 若尝试回退到 planted 应被 DAO 拒绝
        fid = _create_foreshadow(env, name="线", status=FORESHADOW_STATUS_PROGRESSING, plant_chapter_id="ch1", target_chapter_id="ch8")
        reply = json.dumps({
            "updates": [
                {"id": "not-exist", "status": "resolved", "event": "未知id"},
                {"id": fid, "status": "planted", "event": "非法回退"},
            ]
        }, ensure_ascii=False)
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ):
            updated = await update_foreshadows_after_chapter(PROJECT_ID, "ch3", 3, "正文" * 100, _outline())
        assert updated == 0  # 未知 id 被忽略；progressing→planted 回退被 DAO 拒绝
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["status"] == FORESHADOW_STATUS_PROGRESSING

    @pytest.mark.asyncio
    async def test_update_no_active_skips_llm(self, env):
        _insert_project(env, outline=_outline())
        with patch.object(foreshadow_service.llm, "chat", new=AsyncMock()) as mock:
            updated = await update_foreshadows_after_chapter(PROJECT_ID, "ch3", 3, "正文" * 100, _outline())
        assert updated == 0
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_empty_content_returns_zero(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        with patch.object(foreshadow_service.llm, "chat", new=AsyncMock()) as mock:
            updated = await update_foreshadows_after_chapter(PROJECT_ID, "ch3", 3, "", _outline())
        assert updated == 0
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_unexpected_error_degrades_zero(self, env):
        """非 LLM 异常（解析/DB 等）同样不得冒泡——调用方在整书任务主链路里。"""
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(side_effect=RuntimeError("没见过的炸法"))
        ):
            updated = await update_foreshadows_after_chapter(PROJECT_ID, "ch3", 3, "正文" * 100, _outline())
        assert updated == 0


# ═══════════════════════════════════════════════════════════
# 服务：一致性检查 + 展示富化
# ═══════════════════════════════════════════════════════════

class TestConsistencyCheck:
    def test_passed_when_no_issues(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="正常线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        report = check_foreshadow_outline_consistency(PROJECT_ID, _outline())
        assert report["passed"] is True
        assert report["total"] == 0

    def test_out_of_range_and_structural(self, env):
        # 大纲含一个 order=99 的异常章（超出章节总数 4）→ 引用它触发 out_of_range
        outline = {
            "mainSummary": "含异常章的测试大纲",
            "nodes": [
                {"id": "c1", "type": "chapter", "title": "一", "order": 1},
                {"id": "c2", "type": "chapter", "title": "二", "order": 2},
                {"id": "c3", "type": "chapter", "title": "三", "order": 3},
                {"id": "c99", "type": "chapter", "title": "异常章", "order": 99},
            ],
        }
        _insert_project(env, outline=outline)
        _create_foreshadow(env, name="越界线", plant_chapter_id="c1", target_chapter_id="c99")
        _create_foreshadow(env, name="倒置线", plant_chapter_id="c3", target_chapter_id="c1")
        report = check_foreshadow_outline_consistency(PROJECT_ID, outline)
        assert report["passed"] is False
        types = {issue["type"] for issue in report["issues"]}
        assert "out_of_range" in types
        assert "structural" in types

    def test_missing_plant(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="无埋设线", plant_chapter_id="", target_chapter_id="ch8")
        report = check_foreshadow_outline_consistency(PROJECT_ID, _outline())
        assert any(issue["type"] == "missing_plant" for issue in report["issues"])

    def test_missing_and_weak_payoff(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="无回收线", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch4", resolution="")
        _create_foreshadow(env, name="敷衍线", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch5", resolution="揭晓")
        report = check_foreshadow_outline_consistency(PROJECT_ID, _outline())
        types = {issue["type"] for issue in report["issues"]}
        assert "missing_payoff" in types
        assert "weak_payoff" in types

    def test_by_type_counts(self, env):
        _insert_project(env, outline=_outline())
        _create_foreshadow(env, name="线A", status=FORESHADOW_STATUS_RESOLVED, plant_chapter_id="ch1", target_chapter_id="ch4", resolution="")
        report = check_foreshadow_outline_consistency(PROJECT_ID, _outline())
        assert report["by_type"].get("missing_payoff") == 1


class TestEnrichForeshadows:
    def test_enrich_adds_order_and_overdue(self, env):
        _insert_project(env, outline=_outline())
        fid = _create_foreshadow(env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch5")
        foreshadows_dao.update_foreshadow(
            PROJECT_ID, fid,
            status=FORESHADOW_STATUS_PROGRESSING,
            event_note="推进",
            chapter_id="ch3",
            chapter_order=3,
        )
        items = enrich_foreshadows(PROJECT_ID, _outline(), current_chapter_id="ch9")
        assert len(items) == 1
        item = items[0]
        assert item["plant_chapter"] == 1
        assert item["target_chapter"] == 5
        assert item["total_chapters"] == 10
        assert item["overdue"] is True  # 9 > 5+3
        assert item["overdue_by"] == 4
        assert len(item["events"]) == 1
        assert item["events"][0]["note"] == "推进"


# ═══════════════════════════════════════════════════════════
# API：五端点
# ═══════════════════════════════════════════════════════════

class TestForeshadowApi:
    def test_list_returns_structure(self, api_env):
        _insert_project(api_env, outline=_outline())
        _create_foreshadow(api_env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch5")
        with TestClient(app) as client:
            resp = client.get(f"/api/projects/{PROJECT_ID}/foreshadows/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert isinstance(data["items"], list)
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["name"] == "线"
        assert item["projectId"] == PROJECT_ID
        assert item["plantChapter"] == 1
        assert item["targetChapter"] == 5
        assert "consistency" in data
        assert "alertCount" in data

    def test_suggest_returns_candidates(self, api_env):
        _insert_project(api_env, outline=_outline())
        reply = json.dumps([
            {"name": "剑的秘密", "description": "佩剑暗藏遗志", "plant_chapter": 1, "target_chapter": 8}
        ], ensure_ascii=False)
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ), TestClient(app) as client:
            resp = client.post(f"/api/projects/{PROJECT_ID}/foreshadows/suggest")
        assert resp.status_code == 200
        candidates = resp.json()["data"]
        assert len(candidates) == 1
        assert candidates[0]["name"] == "剑的秘密"
        # suggest 端点直接返回 service 候选（snake_case 字段，未走 _to_api 转换）
        assert candidates[0]["plant_chapter_id"] == "ch1"

    def test_suggest_llm_error_degrades_empty(self, api_env):
        _insert_project(api_env, outline=_outline())
        with patch.object(
            foreshadow_service.llm, "chat",
            new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
        ), TestClient(app) as client:
            resp = client.post(f"/api/projects/{PROJECT_ID}/foreshadows/suggest")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_create_returns_created_items(self, api_env):
        _insert_project(api_env, outline=_outline())
        payload = {
            "items": [
                {"name": "伏笔A", "description": "dA", "plantChapterId": "ch1", "targetChapterId": "ch7"},
                {"name": "伏笔B", "description": "dB", "plantChapterId": "ch2", "targetChapterId": "ch9"},
            ]
        }
        with TestClient(app) as client:
            resp = client.post(f"/api/projects/{PROJECT_ID}/foreshadows/", json=payload)
        assert resp.status_code == 200
        created = resp.json()["data"]
        assert len(created) == 2
        assert {item["name"] for item in created} == {"伏笔A", "伏笔B"}
        assert all(item["status"] == "planted" for item in created)

    def test_create_empty_items_returns_400(self, api_env):
        _insert_project(api_env, outline=_outline())
        with TestClient(app) as client:
            resp = client.post(f"/api/projects/{PROJECT_ID}/foreshadows/", json={"items": []})
        assert resp.status_code == 400

    def test_patch_updates_foreshadow(self, api_env):
        _insert_project(api_env, outline=_outline())
        fid = _create_foreshadow(api_env, name="线", status=FORESHADOW_STATUS_PLANTED, plant_chapter_id="ch1", target_chapter_id="ch8")
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/projects/{PROJECT_ID}/foreshadows/{fid}",
                json={"status": "progressing", "description": "新描述"},
            )
        assert resp.status_code == 200
        item = resp.json()["data"]
        assert item["status"] == "progressing"
        assert item["description"] == "新描述"

    def test_patch_not_found_returns_404(self, api_env):
        _insert_project(api_env, outline=_outline())
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/projects/{PROJECT_ID}/foreshadows/not-exist",
                json={"name": "x"},
            )
        assert resp.status_code == 404

    def test_delete_foreshadow(self, api_env):
        _insert_project(api_env, outline=_outline())
        fid = _create_foreshadow(api_env, name="线")
        with TestClient(app) as client:
            resp = client.delete(f"/api/projects/{PROJECT_ID}/foreshadows/{fid}")
        assert resp.status_code == 200
        assert foreshadows_dao.get_foreshadow(PROJECT_ID, fid) is None

    def test_delete_not_found_returns_404(self, api_env):
        _insert_project(api_env, outline=_outline())
        with TestClient(app) as client:
            resp = client.delete(f"/api/projects/{PROJECT_ID}/foreshadows/not-exist")
        assert resp.status_code == 404

    def test_project_not_found_returns_404(self, api_env):
        with TestClient(app) as client:
            assert client.get(f"/api/projects/{OTHER_PROJECT_ID}/foreshadows/").status_code == 404
            assert client.post(f"/api/projects/{OTHER_PROJECT_ID}/foreshadows/suggest").status_code == 404
            assert client.post(
                f"/api/projects/{OTHER_PROJECT_ID}/foreshadows/",
                json={"items": [{"name": "x"}]},
            ).status_code == 404
            assert client.patch(
                f"/api/projects/{OTHER_PROJECT_ID}/foreshadows/any", json={"name": "x"}
            ).status_code == 404
            assert client.delete(f"/api/projects/{OTHER_PROJECT_ID}/foreshadows/any").status_code == 404


# ═══════════════════════════════════════════════════════════
# 服务/API：手写伏笔（作者自己写完正文后登记，AI 只做整理）
# ═══════════════════════════════════════════════════════════

class TestManualForeshadowDraft:
    @pytest.mark.asyncio
    async def test_draft_resolves_chapters_and_keeps_target_pending(self, env):
        _insert_project(env, outline=_outline())
        reply = json.dumps(
            [
                {"name": "剑的秘密", "description": "佩剑暗藏遗志", "plant_chapter": 3, "target_chapter": 0},
                {"name": "没说章号", "description": "作者没写埋设章", "plant_chapter": 0, "target_chapter": 0},
            ],
            ensure_ascii=False,
        )
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ) as mock_chat:
            result = await foreshadow_service.draft_foreshadows_from_text(
                PROJECT_ID, "我把剑的秘密埋在第 3 章，伏笔线就是这把剑。", "ch3"
            )

        assert result["degraded"] is False
        assert result["reason"] == ""
        items = result["items"]
        assert items[0]["plant_chapter"] == 3
        assert items[0]["plant_chapter_id"] == "ch3"
        # 作者没写回收章 → 待定（不替作者猜）
        assert items[0]["target_chapter"] == 0
        assert items[0]["target_chapter_id"] == ""
        # 没写明埋设章 → 默认当前章
        assert items[1]["plant_chapter"] == 3
        # role 必须显式传给路由（否则审计里记成 unspecified）
        assert mock_chat.await_args.kwargs.get("role") == "planning"

    @pytest.mark.asyncio
    async def test_draft_drops_illegal_target_order(self, env):
        _insert_project(env, outline=_outline())
        reply = json.dumps(
            [{"name": "顺序反了", "description": "目标章在埋设章之前", "plant_chapter": 6, "target_chapter": 4}],
            ensure_ascii=False,
        )
        with patch.object(foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)):
            result = await foreshadow_service.draft_foreshadows_from_text(PROJECT_ID, "随便写点", "ch6")
        item = result["items"][0]
        assert item["plant_chapter"] == 6
        assert item["target_chapter"] == 0  # 不合法 → 退回待定
        assert item["target_chapter_id"] == ""

    @pytest.mark.asyncio
    async def test_draft_clamps_out_of_range_order(self, env):
        _insert_project(env, outline=_outline())
        reply = json.dumps(
            [{"name": "越界章", "description": "章号超过全书", "plant_chapter": 99, "target_chapter": 120}],
            ensure_ascii=False,
        )
        with patch.object(foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)):
            result = await foreshadow_service.draft_foreshadows_from_text(PROJECT_ID, "越界测试", "ch1")
        item = result["items"][0]
        assert item["plant_chapter"] == 10  # 10 章大纲 → 裁到末章
        assert item["plant_chapter_id"] == "ch10"
        assert item["target_chapter"] == 0  # 裁到 10 后 <= plant → 待定

    @pytest.mark.asyncio
    async def test_draft_degrades_with_visible_reason(self, env):
        _insert_project(env, outline=_outline())
        with patch.object(
            foreshadow_service.llm, "chat",
            new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
        ):
            result = await foreshadow_service.draft_foreshadows_from_text(PROJECT_ID, "手写伏笔", "ch1")
        assert result["items"] == []
        assert result["degraded"] is True
        assert "模型设置" in result["reason"]

    @pytest.mark.asyncio
    async def test_draft_unparsable_reply_is_not_degraded(self, env):
        _insert_project(env, outline=_outline())
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value="抱歉，我不能这么做。")
        ):
            result = await foreshadow_service.draft_foreshadows_from_text(PROJECT_ID, "手写伏笔", "ch1")
        assert result["items"] == []
        assert result["degraded"] is False  # AI 答了，只是没抽出伏笔
        assert "手动填写" in result["reason"]

    def test_draft_api_rejects_empty_text(self, api_env):
        _insert_project(api_env, outline=_outline())
        with TestClient(app) as client:
            resp = client.post(f"/api/projects/{PROJECT_ID}/foreshadows/draft", json={"text": "   "})
        assert resp.status_code == 400

    def test_draft_api_returns_items(self, api_env):
        _insert_project(api_env, outline=_outline())
        reply = json.dumps(
            [{"name": "手写线", "description": "作者手写登记的伏笔", "plant_chapter": 2, "target_chapter": 7}],
            ensure_ascii=False,
        )
        with patch.object(
            foreshadow_service.llm, "chat", new=AsyncMock(return_value=reply)
        ), TestClient(app) as client:
            resp = client.post(
                f"/api/projects/{PROJECT_ID}/foreshadows/draft",
                json={"text": "我埋了一条手写伏笔", "plantChapterId": "ch2"},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["degraded"] is False
        assert data["items"][0]["name"] == "手写线"
        assert data["items"][0]["plant_chapter_id"] == "ch2"
        assert data["items"][0]["target_chapter_id"] == "ch7"

    def test_patch_with_note_records_event_and_chapter(self, api_env):
        """手动登记：一条 PATCH 同时改状态 + 记下「哪一章、怎么推进的」。"""
        _insert_project(api_env, outline=_outline())
        fid = _create_foreshadow(
            api_env, name="线", status=FORESHADOW_STATUS_PLANTED,
            plant_chapter_id="ch1", target_chapter_id="ch8",
        )
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/projects/{PROJECT_ID}/foreshadows/{fid}",
                json={"status": "resolved", "resolution": "主角认出刻痕", "note": "本章手动回收", "chapterId": "ch4"},
            )
        assert resp.status_code == 200
        events = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["events"]
        assert len(events) == 1
        assert events[0]["note"] == "本章手动回收"
        assert events[0]["chapter_id"] == "ch4"
        assert events[0]["chapter_order"] == 4  # 由大纲解析出来的章序号

    def test_patch_note_only_still_appends_event(self, api_env):
        """只写备注、不改状态，也要留下记录（作者手改正文后的补记场景）。"""
        _insert_project(api_env, outline=_outline())
        fid = _create_foreshadow(
            api_env, name="线", status=FORESHADOW_STATUS_PROGRESSING,
            plant_chapter_id="ch1", target_chapter_id="ch8",
        )
        with TestClient(app) as client:
            resp = client.patch(
                f"/api/projects/{PROJECT_ID}/foreshadows/{fid}",
                json={"note": "本章给了新线索", "chapterId": "ch5"},
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == FORESHADOW_STATUS_PROGRESSING
        events = foreshadows_dao.get_foreshadow(PROJECT_ID, fid)["events"]
        assert len(events) == 1
        assert events[0]["note"] == "本章给了新线索"
        assert events[0]["chapter_order"] == 5
