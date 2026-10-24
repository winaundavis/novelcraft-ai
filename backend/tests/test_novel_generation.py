"""novel_generation_service 单元测试

测试纯逻辑函数（不依赖数据库/API）：
- _chapter_nodes_from_outline
- _select_chapter_nodes (all / current-to-end / first-unfinished)
- _compact_characters
- _compact_world_rules
- _build_context_before
- _build_direction
- _summarize

M1 记忆底座相关（依赖内存库）：
- _build_memory_context 集成（卷摘要限最近 N 卷 / 剧透过滤 / milestones）
- _persist_chapter_and_sync_memory 集成
- P1-2 回归：超长注入块经 contextBefore[-4000:] 截断后 milestones 仍在最终 prompt
"""

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import chapter_commits as cc_dao
from app.dao import chapters as chapters_dao
from app.dao import novel_facts as novel_facts_dao
from app.dao import projects as projects_dao
from app.dao import volumes as volumes_dao
from app.models.schemas import ContinueWritingRequest
from app.services import memory_service
from app.services import foreshadow_service
from app.services import novel_generation_service as ngs
from app.services.ai_service import _build_continue_prompt
from app.services.novel_generation_service import (
    _chapter_nodes_from_outline,
    _select_chapter_nodes,
    _compact_characters,
    _compact_world_rules,
    _build_context_before,
    _build_direction,
    _build_memory_context,
    _build_memory_context_before,
    _persist_chapter_and_sync_memory,
    _summarize,
    NovelGenerationJob,
)

# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def sample_outline():
    return {
        "mainSummary": "测试小说概要",
        "nodes": [
            {"id": "v1", "type": "volume", "title": "卷一", "order": 1},
            {"id": "c1", "type": "chapter", "title": "第一章", "order": 1,
             "summary": "开篇", "conflict": "冲突1", "revelation": "揭示1", "emotion": "紧张",
             "targetWordCount": 2000},
            {"id": "c2", "type": "chapter", "title": "第二章", "order": 2,
             "summary": "发展", "conflict": "冲突2", "revelation": "揭示2", "emotion": "好奇",
             "targetWordCount": 2000},
            {"id": "c3", "type": "chapter", "title": "第三章", "order": 3,
             "summary": "高潮", "conflict": "冲突3", "revelation": "揭示3", "emotion": "紧张",
             "targetWordCount": 2000},
        ]
    }

@pytest.fixture
def sample_characters():
    return [
        {
            "name": "林远", "role": "protagonist",
            "basicInfo": {"identity": "技术员"},
            "surfaceMotivation": "调查真相",
            "deepDesire": "找回记忆",
            "arc": "成长弧线",
            "speechStyle": "简洁理性",
            "habits": "擦拭眼镜",
            "values": "真相至上",
        },
        {
            "name": "陈霜", "role": "supporting",
            "basicInfo": {"identity": "飞行员"},
            "surfaceMotivation": "赚钱",
            "deepDesire": "洗刷冤屈",
            "arc": "找回信念",
        },
    ]

# ── _chapter_nodes_from_outline ───────────────────────────

def test_extracts_only_chapter_nodes(sample_outline):
    nodes = _chapter_nodes_from_outline(sample_outline)
    assert len(nodes) == 3
    assert all(n["type"] == "chapter" for n in nodes)

def test_sorts_by_order(sample_outline):
    # 打乱顺序验证排序
    sample_outline["nodes"] = [
        {"id": "c2", "type": "chapter", "title": "第二章", "order": 2},
        {"id": "c1", "type": "chapter", "title": "第一章", "order": 1},
        {"id": "c3", "type": "chapter", "title": "第三章", "order": 3},
    ]
    nodes = _chapter_nodes_from_outline(sample_outline)
    assert [n["id"] for n in nodes] == ["c1", "c2", "c3"]

# ── _select_chapter_nodes ─────────────────────────────────

def test_mode_all_returns_all(sample_outline):
    nodes = _chapter_nodes_from_outline(sample_outline)
    selected = _select_chapter_nodes("proj1", nodes, "all", None)
    assert len(selected) == 3

def test_mode_current_to_end(sample_outline):
    nodes = _chapter_nodes_from_outline(sample_outline)
    selected = _select_chapter_nodes("proj1", nodes, "current-to-end", "c2")
    assert len(selected) == 2
    assert selected[0]["id"] == "c2"

def test_mode_current_to_end_not_found_returns_all(sample_outline):
    nodes = _chapter_nodes_from_outline(sample_outline)
    selected = _select_chapter_nodes("proj1", nodes, "current-to-end", "nonexist")
    assert len(selected) == 3

# ── _compact_characters ───────────────────────────────────

def test_compacts_to_essential_fields(sample_characters):
    compact = _compact_characters(sample_characters)
    assert len(compact) == 2
    assert compact[0]["name"] == "林远"
    assert "basicInfo" not in compact[0]  # identity 已被提取
    assert compact[0]["identity"] == "技术员"

# ── _compact_world_rules ──────────────────────────────────

def test_compacts_world_rules():
    rules = [
        {"category": "magic", "name": "魔力", "description": "规则描述", "extra_field": "应被移除"},
    ]
    compact = _compact_world_rules(rules)
    assert len(compact) == 1
    assert "extra_field" not in compact[0]
    assert compact[0]["name"] == "魔力"

# ── _build_context_before ─────────────────────────────────

def test_build_context_for_first_chapter(sample_outline):
    ch = sample_outline["nodes"][1]  # c1
    ctx = _build_context_before(
        premise="测试故事", chapter=ch, chapter_index=0, total_chapters=3,
        outline_summary="概要", previous_summary="", existing_text=""
    )
    assert "测试故事" in ctx
    assert "第一章" in ctx
    assert "开篇建立悬念" in ctx.lower() or "起始" in ctx

def test_build_context_with_existing_text(sample_outline):
    ch = sample_outline["nodes"][1]
    ctx = _build_context_before(
        premise="测试", chapter=ch, chapter_index=0, total_chapters=3,
        outline_summary="概要", previous_summary="", existing_text="已有内容"
    )
    assert ctx == "已有内容"


def test_scoped_regeneration_does_not_reinject_tainted_existing_text(sample_outline):
    contract = {"arc_context": {"outline_scope": {"arc_title": "当前弧", "arc_summary": "只处理当前事件"}}}
    ctx = _build_context_before(
        premise="测试", chapter=sample_outline["nodes"][1], chapter_index=0,
        total_chapters=3, outline_summary="当前弧", existing_text="TAINTED_FUTURE_ARC",
        memory_context={"volume_summaries": ["FUTURE_VOLUME"], "active_foreshadows": "FUTURE_FORESHADOW",
                        "timeline": "FUTURE_TIMELINE", "completed_milestones": "FUTURE_STATE"},
        chapter_contract=contract,
    )
    assert "TAINTED_FUTURE_ARC" not in ctx
    assert "FUTURE_" not in ctx
    assert "当前弧" in ctx

def test_build_context_for_middle_chapter(sample_outline):
    ch = sample_outline["nodes"][2]  # c2
    ctx = _build_context_before(
        premise="测试", chapter=ch, chapter_index=1, total_chapters=3,
        outline_summary="概要", previous_summary="前章摘要", existing_text=""
    )
    assert "关系与线索铺垫" in ctx or "第二章" in ctx

# ── _build_direction ──────────────────────────────────────

def test_build_direction_includes_title():
    ch = {"title": "第一章", "revelation": "秘密", "emotion": "紧张"}
    direction = _build_direction(ch)
    assert "第一章" in direction
    assert "秘密" in direction

# ── _summarize ────────────────────────────────────────────

def test_summarize_truncates_long_text():
    long_text = "这是一个" + "很长很长的" * 50 + "测试文本"
    result = _summarize(long_text, max_length=30)
    assert len(result) <= 30 + 3  # +3 for "..."

def test_summarize_short_text():
    result = _summarize("短文本")
    assert result == "短文本"

def test_summarize_empty():
    result = _summarize("")
    assert result == ""


# ── _build_context_before（M1 分层记忆注入） ─────────────

def test_build_context_before_with_memory_context(sample_outline):
    """传入 memory_context 时注入：已完成事实 + 卷摘要 + 近 5 章摘要 + 上一章结尾。"""
    ch = sample_outline["nodes"][2]  # c2
    memory_context = {
        "volume_summaries": ["第一卷：主角进入宗门，掌握御剑术。"],
        "recent_chapter_summaries": ["第一章摘要", "第二章摘要"],
        "last_chapter_ending": "……他推开了那扇尘封的大门。",
        "completed_milestones": "【已完成事实】以下事件已经发生且不可撤销：\n- 主角取得青云令",
    }
    ctx = _build_context_before(
        premise="测试", chapter=ch, chapter_index=1, total_chapters=3,
        outline_summary="概要", previous_summary="旧单链摘要", existing_text="",
        memory_context=memory_context,
    )
    assert "已完成事实" in ctx
    assert "主角取得青云令" in ctx
    assert "第一卷：主角进入宗门" in ctx
    assert "第一章摘要" in ctx and "第二章摘要" in ctx
    assert "他推开了那扇尘封的大门" in ctx
    # 不应再使用旧单链 previous_summary 文案
    assert "旧单链摘要" not in ctx


def test_build_context_before_empty_memory_falls_back_to_legacy(sample_outline):
    """memory_context 全空时回退单链 previous_summary（老数据兼容）。"""
    ch = sample_outline["nodes"][2]  # c2
    ctx = _build_context_before(
        premise="测试", chapter=ch, chapter_index=1, total_chapters=3,
        outline_summary="概要", previous_summary="前章摘要", existing_text="",
        memory_context={
            "volume_summaries": [],
            "recent_chapter_summaries": [],
            "last_chapter_ending": "",
            "completed_milestones": "",
        },
    )
    assert "前章摘要" in ctx


def test_build_context_before_first_chapter_with_memory_context(sample_outline):
    """起始章节：无上一章结尾时给出起始提示，但仍注入卷摘要等可用记忆。"""
    ch = sample_outline["nodes"][1]  # c1
    memory_context = {
        "volume_summaries": ["第一卷：主角进入宗门"],  # 非空记忆 → 走记忆注入路径
        "recent_chapter_summaries": [],
        "last_chapter_ending": "",
        "completed_milestones": "",
    }
    ctx = _build_context_before(
        premise="测试", chapter=ch, chapter_index=0, total_chapters=3,
        outline_summary="概要", previous_summary="", existing_text="",
        memory_context=memory_context,
    )
    assert "这是本书较早的章节" in ctx
    assert "第一卷：主角进入宗门" in ctx


# ══════════════════════════════════════════════════════
# M1 记忆底座集成测试（多表内存库）
# ══════════════════════════════════════════════════════

@pytest.fixture
def full_memory_db():
    """多表内存库（projects/chapters/volumes/novel_facts），patch 相关 DAO 的 get_conn。

    用于 _build_memory_context / _persist_chapter_and_sync_memory / P1-2 截断链路集成测试。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            subtitle TEXT NOT NULL DEFAULT '',
            genre TEXT NOT NULL DEFAULT 'xuanhuan',
            target_word_count INTEGER NOT NULL DEFAULT 300000,
            current_word_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planning',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
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
            updated_at TEXT NOT NULL
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

        -- P1：不可变提交链 + 记忆投影状态表（正文落库同事务写入 commit）
        CREATE TABLE IF NOT EXISTS chapter_commit_history (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            parent_commit_id TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            context_hash TEXT NOT NULL DEFAULT '',
            audit_id TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(project_id, chapter_id, version)
        );
        CREATE TABLE IF NOT EXISTS memory_projections (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL,
            projection_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, chapter_id, projection_type)
        );
        """
    )
    conn.commit()

    def _get_conn():
        return conn

    with patch.object(ngs, "get_conn", _get_conn), \
         patch.object(chapters_dao, "get_conn", _get_conn), \
         patch.object(volumes_dao, "get_conn", _get_conn), \
         patch.object(novel_facts_dao, "get_conn", _get_conn), \
         patch.object(projects_dao, "get_conn", _get_conn), \
         patch.object(cc_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


def _insert_project_for_memory_db(project_id: str = "proj-1") -> None:
    projects_dao.create_project({
        "id": project_id,
        "name": "测试小说",
        "subtitle": "",
        "genre": "xuanhuan",
        "target_word_count": 300000,
        "current_word_count": 0,
        "status": "planning",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "style_config": "{}",
        "outline": "null",
        "tags": "[]",
        "is_starred": 0,
        "premise": "测试",
        "target_chapters": 30,
        "characters": "[]",
        "locations": "[]",
        "world_rules": "[]",
        "import_analysis": "null",
        "inspiration_fragments": "[]",
    })


# ── _build_memory_context 集成 ─────────────────────────

def test_build_memory_context_integration(full_memory_db):
    """集成：只注入最近 5 卷摘要、只注入当前章之前的摘要（无剧透）、milestones 含已完成事实。"""
    _insert_project_for_memory_db()

    # 10 卷（order 1..10），每卷有摘要
    for i in range(1, 11):
        volumes_dao.upsert_volume(
            "proj-1", f"v{i}", title=f"第{i}卷", volume_order=i, summary=f"第{i}卷摘要内容" * 20,
        )
    # 12 章（order 1..12），ch-9 有正文作为上一章
    for i in range(1, 13):
        chapters_dao.insert_chapter({
            "id": f"ch-{i}",
            "project_id": "proj-1",
            "volume_id": f"v{(i - 1) // 10 + 1}",
            "title": f"第{i}章",
            "chapter_order": i,
            "content": f"第{i}章正文" * 200 if i == 9 else "",
            "word_count": 0,
            "status": "completed",
            "summary": f"第{i}章摘要",
            "notes": "",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        })
    novel_facts_dao.add_fact("proj-1", "completed", "主角取得青云令", "ch-1")

    chapter_nodes = [
        {"id": f"ch-{i}", "type": "chapter", "order": i, "title": f"第{i}章"} for i in range(1, 13)
    ]
    ctx = _build_memory_context(
        project_id="proj-1", chapter_nodes=chapter_nodes, current_chapter_id="ch-10",
    )

    # 卷摘要：只注入最近 5 卷（v6..v10），早期卷不挤占
    assert len(ctx["volume_summaries"]) == 5
    assert ctx["volume_summaries"][0].startswith("第6卷摘要内容")
    assert ctx["volume_summaries"][-1].startswith("第10卷摘要内容")

    # 最近章节摘要：只含当前章（ch-10，order=10）之前的最近 5 条 → ch-5..ch-9
    assert len(ctx["recent_chapter_summaries"]) == 5
    assert ctx["recent_chapter_summaries"][0] == "第9章摘要"  # 最新的在前
    assert not any(s in {"第10章摘要", "第11章摘要", "第12章摘要"} for s in ctx["recent_chapter_summaries"])

    # 上一章结尾：ch-9 正文尾部（≤ 800 字）
    assert ctx["last_chapter_ending"].endswith("第9章正文")
    assert len(ctx["last_chapter_ending"]) <= memory_service.LAST_ENDING_LENGTH

    # 已完成事实
    assert "主角取得青云令" in ctx["completed_milestones"]


# ── _persist_chapter_and_sync_memory 集成 ──────────────

@pytest.mark.asyncio
async def test_persist_chapter_and_sync_memory(full_memory_db):
    """集成：章节落库 + 章摘要写入 + 记忆同步（事实抽取 + 卷摘要压缩）一次完成。"""
    _insert_project_for_memory_db()
    job = NovelGenerationJob(
        id="job-1", project_id="proj-1", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=3000, total_chapters=1,
    )
    chapter = {"id": "ch-1", "title": "第一章", "parentId": "v1", "order": 1, "summary": ""}
    result = {
        "content": "这是第一章的正文，用于落库测试。",
        "used_fallback": False,
        "provider": "mock",
        "model": "mock-model",
        "last_source": "llm",
    }
    outline = {"nodes": [{"id": "v1", "type": "volume", "title": "卷一", "order": 1}]}

    with patch.object(
        memory_service, "summarize_chapter", new=AsyncMock(return_value="本章摘要")
    ) as mock_sum, \
         patch.object(
             memory_service, "extract_facts", new=AsyncMock(return_value=[])
         ) as mock_extract, \
         patch.object(
             memory_service, "record_chapter_completed", new=AsyncMock()
         ) as mock_record:
        summary = await _persist_chapter_and_sync_memory(
            job=job, chapter=chapter, result=result, outline=outline,
        )

    assert summary == "本章摘要"
    persisted = chapters_dao.get_chapter("ch-1", "proj-1")
    assert persisted is not None
    assert persisted["content"] == result["content"]
    assert persisted["status"] == "completed"
    assert persisted["summary"] == "本章摘要"
    assert persisted["wordCount"] == 16  # _count_words("这是第一章的正文，用于落库测试。")
    mock_sum.assert_awaited_once()
    mock_extract.assert_awaited_once()
    mock_record.assert_awaited_once()
    # 项目字数同步更新
    assert projects_dao.get_project("proj-1")["currentWordCount"] == 16


# ── 伏笔账本接线：章节落库后自动追踪 ──────────────────

def _set_foreshadow_tracking(monkeypatch, enabled: bool) -> None:
    """显式设置追踪开关（默认**关**：见 config.foreshadow_auto_track 的成本注释）。"""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "foreshadow_auto_track", enabled)


@pytest.mark.asyncio
async def test_persist_calls_foreshadow_tracking(full_memory_db, monkeypatch):
    """接线：正文落库后自动追踪活跃伏笔（不再依赖人工维护状态）。"""
    _set_foreshadow_tracking(monkeypatch, True)
    _insert_project_for_memory_db()
    job = NovelGenerationJob(
        id="job-1", project_id="proj-1", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=3000, total_chapters=1,
    )
    chapter = {"id": "ch-1", "title": "第一章", "parentId": "v1", "order": 1, "summary": ""}
    result = {
        "content": "第一章正文",
        "used_fallback": False,
        "provider": "mock",
        "model": "mock-model",
        "last_source": "llm",
    }
    outline = {"nodes": [{"id": "v1", "type": "volume", "title": "卷一", "order": 1}]}

    with patch.object(
        memory_service, "summarize_chapter", new=AsyncMock(return_value="本章摘要")
    ), patch.object(
        memory_service, "extract_facts", new=AsyncMock(return_value=[])
    ), patch.object(
        memory_service, "record_chapter_completed", new=AsyncMock()
    ), patch.object(
        foreshadow_service, "update_foreshadows_after_chapter", new=AsyncMock(return_value=1)
    ) as mock_track:
        summary = await _persist_chapter_and_sync_memory(
            job=job, chapter=chapter, result=result, outline=outline,
        )

    assert summary == "本章摘要"
    mock_track.assert_awaited_once()
    args = mock_track.await_args.args
    assert args[0] == "proj-1"
    assert args[1] == "ch-1"
    assert args[2] == 1  # 章序号来自 chapter.order
    assert args[3] == "第一章正文"


@pytest.mark.asyncio
async def test_persist_skips_foreshadow_tracking_when_switch_off(full_memory_db, monkeypatch):
    """FORESHADOW_AUTO_TRACK=false（默认）时不做追踪：省掉每章那次额外 LLM 调用。"""
    _set_foreshadow_tracking(monkeypatch, False)
    _insert_project_for_memory_db()
    job = NovelGenerationJob(
        id="job-1", project_id="proj-1", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=3000, total_chapters=1,
    )
    chapter = {"id": "ch-1", "title": "第一章", "parentId": "v1", "order": 1, "summary": ""}
    result = {
        "content": "第一章正文",
        "used_fallback": False,
        "provider": "mock",
        "model": "mock-model",
        "last_source": "llm",
    }
    outline = {"nodes": [{"id": "v1", "type": "volume", "title": "卷一", "order": 1}]}

    with patch.object(
        memory_service, "summarize_chapter", new=AsyncMock(return_value="本章摘要")
    ), patch.object(
        memory_service, "extract_facts", new=AsyncMock(return_value=[])
    ), patch.object(
        memory_service, "record_chapter_completed", new=AsyncMock()
    ), patch.object(
        foreshadow_service, "update_foreshadows_after_chapter", new=AsyncMock(return_value=1)
    ) as mock_track:
        summary = await _persist_chapter_and_sync_memory(
            job=job, chapter=chapter, result=result, outline=outline,
        )

    assert summary == "本章摘要"
    mock_track.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_fallback_skips_foreshadow_tracking(full_memory_db, monkeypatch):
    """占位（fallback）正文不代表真实剧情推进，不参与伏笔追踪（即使开关是开的）。"""
    _set_foreshadow_tracking(monkeypatch, True)
    _insert_project_for_memory_db()
    job = NovelGenerationJob(
        id="job-1", project_id="proj-1", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=3000, total_chapters=1,
    )
    chapter = {"id": "ch-1", "title": "第一章", "parentId": "v1", "order": 1, "summary": ""}
    result = {
        "content": "占位正文",
        "used_fallback": True,
        "provider": "fallback",
        "model": "",
        "last_source": "placeholder",
    }
    outline = {"nodes": [{"id": "v1", "type": "volume", "title": "卷一", "order": 1}]}

    with patch.object(
        memory_service, "summarize_chapter", new=AsyncMock(return_value="")
    ), patch.object(
        memory_service, "extract_facts", new=AsyncMock(return_value=[])
    ), patch.object(
        memory_service, "record_chapter_completed", new=AsyncMock()
    ), patch.object(
        foreshadow_service, "update_foreshadows_after_chapter", new=AsyncMock(return_value=1)
    ) as mock_track:
        await _persist_chapter_and_sync_memory(
            job=job, chapter=chapter, result=result, outline=outline,
        )

    mock_track.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_survives_foreshadow_tracking_crash(full_memory_db, monkeypatch):
    """追踪爆掉也不得冒泡——整书任务主链路只允许降级，不允许阻塞。"""
    _set_foreshadow_tracking(monkeypatch, True)
    _insert_project_for_memory_db()
    job = NovelGenerationJob(
        id="job-1", project_id="proj-1", mode="all", requested_chapter_id=None,
        status="running", message="", target_words=3000, total_chapters=1,
    )
    chapter = {"id": "ch-1", "title": "第一章", "parentId": "v1", "order": 1, "summary": ""}
    result = {
        "content": "第一章正文",
        "used_fallback": False,
        "provider": "mock",
        "model": "mock-model",
        "last_source": "llm",
    }
    outline = {"nodes": [{"id": "v1", "type": "volume", "title": "卷一", "order": 1}]}

    with patch.object(
        memory_service, "summarize_chapter", new=AsyncMock(return_value="本章摘要")
    ), patch.object(
        memory_service, "extract_facts", new=AsyncMock(return_value=[])
    ), patch.object(
        memory_service, "record_chapter_completed", new=AsyncMock()
    ), patch.object(
        foreshadow_service,
        "update_foreshadows_after_chapter",
        new=AsyncMock(side_effect=RuntimeError("追踪爆炸")),
    ):
        summary = await _persist_chapter_and_sync_memory(
            job=job, chapter=chapter, result=result, outline=outline,
        )

    assert summary == "本章摘要"
    assert chapters_dao.get_chapter("ch-1", "proj-1")["content"] == "第一章正文"


# ── P1-2 回归：超长注入块截断后 milestones 不丢 ────────

def test_continue_prompt_keeps_milestones_after_truncation(full_memory_db):
    """回归（P1-2）：模拟 400 章事实累积的超长注入块，经 contextBefore[-4000:]
    截断后【已完成事实】块仍完整保留在最终 prompt 中。

    修复前 milestones 位于注入块中部被整体截断丢弃；修复后置后 + 限长 + 卷摘要限流。
    """
    counter = {"n": 0}

    def _tick():
        counter["n"] += 1
        n = counter["n"]
        return f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z"

    # 400 章事实累积
    with patch.object(novel_facts_dao, "_utc_iso", _tick):
        for i in range(400):
            novel_facts_dao.add_fact(
                "proj-1", "completed", f"第{i}章完成的关键事实：主角取得第{i}件宝物", f"ch-{i}"
            )

    # 大量卷摘要（验证卷摘要限流对保留窗口的贡献）
    for i in range(40):
        volumes_dao.upsert_volume(
            "proj-1", f"v{i}", title=f"第{i}卷", volume_order=i, summary=f"第{i}卷摘要内容" * 30,
        )

    milestones = memory_service.build_completed_milestones("proj-1")
    assert len(milestones) <= memory_service.MAX_MILESTONES_LENGTH

    memory_context = {
        "volume_summaries": volumes_dao.list_volume_summaries("proj-1")[
            -memory_service.RECENT_VOLUME_SUMMARIES:
        ],
        "recent_chapter_summaries": ["近五章摘要内容" * 20] * 5,
        "last_chapter_ending": "上一章结尾的悬念内容。" * 100,
        "completed_milestones": milestones,
    }
    chapter = {
        "id": "ch-50", "title": "第五十章", "summary": "推进",
        "conflict": "冲突", "revelation": "揭示", "emotion": "紧张",
    }
    context_before = _build_memory_context_before(
        premise="测试小说", chapter=chapter, chapter_index=49, total_chapters=100,
        outline_summary="全局大纲摘要", memory_context=memory_context,
    )
    assert len(context_before) > 4000  # 确认构造的是超长注入块

    req = ContinueWritingRequest(
        projectId="proj-1",
        chapterId="ch-50",
        cursorPosition=0,
        contextBefore=context_before,
        direction="请续写。",
        wordCount=2000,
        chapterTitle="第五十章",
        premise="测试小说",
        outlineSummary="全局大纲摘要",
        chapterSummary="推进",
        styleTone="冷峻",
        characters=[],
        worldRules=[],
    )
    prompt = _build_continue_prompt(req)
    # 截断后 milestones 块与最新事实仍保留
    assert "【已完成事实】" in prompt
    assert "第399章完成的关键事实" in prompt

