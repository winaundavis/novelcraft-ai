"""memory_service（M1 记忆底座）单元测试。

覆盖：
- summarize_chapter：LLM 正常 / LLM 失败降级截断 / 空内容
- extract_facts：JSON 解析入库 / LLM 失败跳过不阻塞 / 代码块围栏鲁棒解析
- _parse_facts：非法类型归一 / 非数组 / 坏 JSON
- build_completed_milestones：completed+revealed 全量 + state_changed 最近 N 条
- build_volume_specs：大纲卷节点优先 / 无卷节点按 10 章切分
- compress_volume_summary：LLM 正常 / LLM 失败拼接降级
- record_chapter_completed：卷末章触发压缩 / 非卷末不触发

LLM 全部 mock，不发起真实网络请求。
"""

from __future__ import annotations

import sqlite3
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import chapters as chapters_dao
from app.dao import novel_facts as novel_facts_dao
from app.dao import volumes as volumes_dao
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.services import memory_service


# ══════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════

@pytest.fixture
def memory_db(tmp_path):
    """独立 SQLite 临时文件库（novel_facts 表），patch novel_facts_dao.get_conn。

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
        new_conn = sqlite3.connect(db_path)
        new_conn.row_factory = sqlite3.Row
        return new_conn

    with patch.object(novel_facts_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


@pytest.fixture
def sample_outline():
    """带卷节点的示例大纲。"""
    return {
        "mainSummary": "测试概要",
        "nodes": [
            {"id": "v1", "type": "volume", "title": "第一卷", "order": 1},
            {"id": "c1", "type": "chapter", "parentId": "v1", "title": "第一章", "order": 1},
            {"id": "c2", "type": "chapter", "parentId": "v1", "title": "第二章", "order": 2},
            {"id": "c3", "type": "chapter", "parentId": "v1", "title": "第三章", "order": 3},
            {"id": "v2", "type": "volume", "title": "第二卷", "order": 2},
            {"id": "c4", "type": "chapter", "parentId": "v2", "title": "第四章", "order": 4},
        ],
    }


@pytest.fixture
def outline_without_volumes():
    """无卷节点的示例大纲（触发每 10 章一卷的兜底切分）。"""
    return {
        "nodes": [
            {"id": f"c{i}", "type": "chapter", "title": f"第{i}章", "order": i}
            for i in range(1, 23)
        ]
    }


# ══════════════════════════════════════════════════════
# summarize_chapter
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_summarize_chapter_uses_llm():
    with patch.object(memory_service.llm, "chat", new=AsyncMock(return_value="主角在废墟中找到信物。")) as mock_chat:
        result = await memory_service.summarize_chapter("正文内容……", "第一章")
    assert result == "主角在废墟中找到信物。"
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_summarize_chapter_truncates_long_reply():
    long_reply = "很长" * 500
    with patch.object(memory_service.llm, "chat", new=AsyncMock(return_value=long_reply)):
        result = await memory_service.summarize_chapter("正文", "第一章")
    assert len(result) <= memory_service.CHAPTER_SUMMARY_MAX


@pytest.mark.asyncio
async def test_summarize_chapter_falls_back_on_llm_error():
    content = "这是一个" + "很长很长的" * 100 + "测试章节内容"
    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=LLMResponseError("调用失败"))):
        result = await memory_service.summarize_chapter(content, "第一章")
    # 降级为截断摘要，不抛异常
    assert result
    assert len(result) <= memory_service.CHAPTER_SUMMARY_MAX + 3


@pytest.mark.asyncio
async def test_summarize_chapter_empty_content():
    result = await memory_service.summarize_chapter("   ")
    assert result == ""


# ══════════════════════════════════════════════════════
# extract_facts / _parse_facts
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_extract_facts_parses_and_inserts(memory_db):
    reply = (
        '[{"fact_type": "completed", "content": "主角取得青云令"},'
        ' {"fact_type": "revealed", "content": "幕后主使身份揭晓"}]'
    )
    with patch.object(memory_service.llm, "chat", new=AsyncMock(return_value=reply)):
        inserted = await memory_service.extract_facts("proj-1", "ch-1", "正文……")
    assert len(inserted) == 2
    assert novel_facts_dao.count_facts("proj-1") == 2


@pytest.mark.asyncio
async def test_extract_facts_llm_error_returns_empty(memory_db):
    """LLM 失败时跳过抽取，不阻塞任务。"""
    with patch.object(
        memory_service.llm, "chat", new=AsyncMock(side_effect=LLMConfigurationError("未配置"))
    ):
        inserted = await memory_service.extract_facts("proj-1", "ch-1", "正文……")
    assert inserted == []
    assert novel_facts_dao.count_facts("proj-1") == 0


@pytest.mark.asyncio
async def test_extract_facts_handles_fenced_json(memory_db):
    reply = "```json\n[{\"fact_type\": \"completed\", \"content\": \"契约签订\"}]\n```"
    with patch.object(memory_service.llm, "chat", new=AsyncMock(return_value=reply)):
        inserted = await memory_service.extract_facts("proj-1", "ch-1", "正文……")
    assert len(inserted) == 1
    assert inserted[0]["content"] == "契约签订"


def test_parse_facts_normalizes_invalid_type():
    facts = memory_service._parse_facts(
        '[{"fact_type": "weird", "content": "某事件"}, {"fact_type": "state_changed", "content": "死亡"}]'
    )
    assert facts[0]["fact_type"] == "completed"
    assert facts[1]["fact_type"] == "state_changed"


def test_parse_facts_returns_empty_for_non_array():
    assert memory_service._parse_facts('{"result": "ok"}') == []
    assert memory_service._parse_facts("no json here") == []
    assert memory_service._parse_facts("") == []


def test_parse_facts_limits_count_and_length():
    reply = "[" + ",".join(
        f'{{"fact_type": "completed", "content": "事实{i}"}}' for i in range(20)
    ) + "]"
    facts = memory_service._parse_facts(reply)
    assert len(facts) == memory_service.MAX_FACT_ITEMS


# ══════════════════════════════════════════════════════
# build_completed_milestones
# ══════════════════════════════════════════════════════

def test_build_completed_milestones_includes_all_types(memory_db):
    novel_facts_dao.add_fact("proj-1", "completed", "完成契约", "ch-1")
    novel_facts_dao.add_fact("proj-1", "revealed", "身世揭晓", "ch-2")
    novel_facts_dao.add_fact("proj-1", "state_changed", "陈墨死亡", "ch-3")
    block = memory_service.build_completed_milestones("proj-1")
    assert "完成契约" in block
    assert "身世揭晓" in block
    assert "陈墨死亡" in block
    assert "不要再重复描写" in block


def test_build_completed_milestones_empty(memory_db):
    assert memory_service.build_completed_milestones("proj-1") == ""


def test_build_completed_milestones_enforces_length_limit(memory_db):
    """回归（P1-2）：长书场景（400 章事实累积）下注入块总长不超上限，且保留最近事实。"""
    counter = {"n": 0}

    def _tick():
        counter["n"] += 1
        n = counter["n"]
        return f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z"

    with patch.object(novel_facts_dao, "_utc_iso", _tick):
        for i in range(400):
            novel_facts_dao.add_fact(
                "proj-1", "completed", f"第{i}章完成的关键事实：主角取得第{i}件宝物", f"ch-{i}"
            )

    block = memory_service.build_completed_milestones("proj-1")
    assert len(block) <= memory_service.MAX_MILESTONES_LENGTH
    # 保留最近（最后插入）的事实
    assert "第399章完成的关键事实" in block
    # 400 条远超上限，最早的条目必然被截断
    assert "第0章完成的关键事实" not in block
    # 输出仍以标题开头（结构完整）
    assert block.startswith("【已完成事实】")


def test_build_completed_milestones_keeps_chronological_order(memory_db):
    """回归（P1-2）：截断后剩余条目仍按时间正序输出（先发生的在前）。"""
    counter = {"n": 0}

    def _tick():
        counter["n"] += 1
        n = counter["n"]
        return f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z"

    with patch.object(novel_facts_dao, "_utc_iso", _tick):
        for i in range(400):
            novel_facts_dao.add_fact(
                "proj-1", "completed", f"事实{i}", f"ch-{i}"
            )

    block = memory_service.build_completed_milestones("proj-1")
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(lines) >= 2
    first_content = lines[0][2:]
    last_content = lines[-1][2:]
    # 正序：第一行是保留范围内最早的事实，最后一行是最新的事实
    # 注意内容形如"事实217"，需提取数字后再比较
    def _extract_num(text: str) -> int:
        return int("".join(ch for ch in text if ch.isdigit()))

    assert _extract_num(first_content) < _extract_num(last_content)


# ══════════════════════════════════════════════════════
# build_volume_specs
# ══════════════════════════════════════════════════════

def test_build_volume_specs_from_outline_volumes(sample_outline):
    specs = memory_service.build_volume_specs(sample_outline)
    assert len(specs) == 2
    assert specs[0]["volume_id"] == "v1"
    assert specs[0]["start_chapter_id"] == "c1"
    assert specs[0]["end_chapter_id"] == "c3"
    assert specs[1]["volume_id"] == "v2"
    assert specs[1]["end_chapter_id"] == "c4"


def test_build_volume_specs_fallback_every_10_chapters(outline_without_volumes):
    specs = memory_service.build_volume_specs(outline_without_volumes)
    assert len(specs) == 3  # 22 章 → 10 + 10 + 2
    assert specs[0]["volume_id"] == "vol-1"
    assert specs[0]["end_chapter_id"] == "c10"
    assert specs[1]["volume_id"] == "vol-2"
    assert specs[2]["volume_id"] == "vol-3"
    assert specs[2]["chapter_ids"] == ["c21", "c22"]


# ══════════════════════════════════════════════════════
# compress_volume_summary
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_compress_volume_summary_uses_llm():
    chapter_summaries = [
        {"chapter_id": "c1", "summary": "第一章摘要"},
        {"chapter_id": "c2", "summary": "第二章摘要"},
    ]
    long_summary = "一卷的压缩摘要内容。" * 20  # ≥100 字，满足卷摘要长度保护阈值
    with patch.object(chapters_dao, "list_summaries_by_ids", return_value=chapter_summaries), \
         patch.object(memory_service.llm, "chat", new=AsyncMock(return_value=long_summary)):
        result = await memory_service.compress_volume_summary("proj-1", "v1", ["c1", "c2"])
    assert result == long_summary


@pytest.mark.asyncio
async def test_compress_volume_summary_falls_back_to_join():
    chapter_summaries = [
        {"chapter_id": "c1", "summary": "第一章摘要内容"},
        {"chapter_id": "c2", "summary": "第二章摘要内容"},
    ]
    with patch.object(chapters_dao, "list_summaries_by_ids", return_value=chapter_summaries), \
         patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=LLMResponseError("失败"))):
        result = await memory_service.compress_volume_summary("proj-1", "v1", ["c1", "c2"])
    assert "第一章摘要内容" in result
    assert "第二章摘要内容" in result


@pytest.mark.asyncio
async def test_compress_volume_summary_no_summaries():
    with patch.object(chapters_dao, "list_summaries_by_ids", return_value=[]):
        result = await memory_service.compress_volume_summary("proj-1", "v1", ["c1", "c2"])
    assert result == ""


# ══════════════════════════════════════════════════════
# record_chapter_completed
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_record_chapter_completed_triggers_on_volume_end(sample_outline):
    volume_summary = "第一卷卷摘要。" * 30  # ≥100 字，满足卷摘要长度保护阈值
    with patch.object(volumes_dao, "get_volume", return_value={"summary": ""}), \
         patch.object(chapters_dao, "list_summaries_by_ids", return_value=[
             {"chapter_id": "c1", "summary": "一"}, {"chapter_id": "c2", "summary": "二"},
             {"chapter_id": "c3", "summary": "三"},
         ]), \
         patch.object(memory_service.llm, "chat", new=AsyncMock(return_value=volume_summary)), \
         patch.object(volumes_dao, "update_volume_summary") as mock_update:
        await memory_service.record_chapter_completed("proj-1", sample_outline, "c3")
    # 记忆升级 v1：写入卷摘要时一并写入"输入签名"（summary_hash），供摘要对账漂移检测
    mock_update.assert_called_once()
    args, kwargs = mock_update.call_args
    assert args == ("proj-1", "v1", volume_summary)
    assert kwargs.get("summary_hash")  # 卷内章摘要拼接的确定性 MD5 签名


@pytest.mark.asyncio
async def test_record_chapter_completed_skips_non_volume_end(sample_outline):
    with patch.object(volumes_dao, "get_volume") as mock_get, \
         patch.object(volumes_dao, "update_volume_summary") as mock_update:
        await memory_service.record_chapter_completed("proj-1", sample_outline, "c2")
    mock_get.assert_not_called()
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_record_chapter_completed_keeps_existing_summary(sample_outline):
    """卷摘要已生成且签名一致时不再重复压缩（懒压缩幂等，验收 P1 摘要对账）。"""
    with patch.object(volumes_dao, "get_volume", return_value={"summary": "已有卷摘要", "summary_hash": "hash-x"}), \
         patch.object(memory_service, "reconcile_volume_summaries", new=AsyncMock()) as mock_reconcile, \
         patch.object(volumes_dao, "update_volume_summary") as mock_update:
        await memory_service.record_chapter_completed("proj-1", sample_outline, "c3")
    # 有摘要的卷：不重复压缩；卷完结时触发对账（修复潜在漂移）
    mock_update.assert_not_called()
    mock_reconcile.assert_awaited_once_with("proj-1", apply=True)


@pytest.mark.asyncio
async def test_record_chapter_completed_reconciles_drift_on_volume_end(sample_outline):
    """卷完结时对已有摘要的卷触发自动对账（验收 P1：摘要对账自动化）。"""
    with patch.object(volumes_dao, "get_volume", return_value={"summary": "旧卷摘要", "summary_hash": "old-hash"}), \
         patch.object(memory_service, "reconcile_volume_summaries", new=AsyncMock()) as mock_reconcile:
        await memory_service.record_chapter_completed("proj-1", sample_outline, "c3")
    mock_reconcile.assert_awaited_once_with("proj-1", apply=True)

# ���� ������ʵì�����⣨Codex P2 ���� P1�� ����

@pytest.mark.asyncio
async def test_resolve_cross_chapter_conflicts_supersedes_old_fact(memory_db):
    """����ʵ�Ʒ�����ʵʱ������ʵ����� superseded��"""
    # ��д�����ʵ�������½ڣ�
    novel_facts_dao.add_fact("proj-1", "completed", "�ر����ǳ¾�", "ch-1")
    novel_facts_dao.add_fact("proj-1", "completed", "��īȡ��ľ��", "ch-1")
    old_facts = novel_facts_dao.list_facts("proj-1")
    old_id = next(f["id"] for f in old_facts if f["content"] == "�ر����ǳ¾�")

    # ����ʵ���ر����ǳ����ߣ��Ʒ����趨��
    new_facts = [{"fact_type": "revealed", "content": "�ر����ǳ�����"}]
    reply = '{"supersede_ids": ["' + old_id + '"]}'
    with patch.object(memory_service.llm, "chat", new=AsyncMock(return_value=reply)):
        count = await memory_service.resolve_cross_chapter_fact_conflicts("proj-1", new_facts)
    assert count == 1

    facts = novel_facts_dao.list_facts("proj-1")
    by_id = {f["id"]: f for f in facts}
    assert by_id[old_id]["status"] == "superseded"
    assert by_id[old_id]["content"] == "�ر����ǳ¾�"


@pytest.mark.asyncio
async def test_resolve_cross_chapter_conflicts_no_conflict(memory_db):
    """��ì��ʱ���� 0����ʧЧ�κ���ʵ��"""
    novel_facts_dao.add_fact("proj-1", "completed", "��īȡ��ľ��", "ch-1")
    new_facts = [{"fact_type": "completed", "content": "��īǰ������"}]
    with patch.object(memory_service.llm, "chat", new=AsyncMock(return_value='{"supersede_ids": []}')):
        count = await memory_service.resolve_cross_chapter_fact_conflicts("proj-1", new_facts)
    assert count == 0
    facts = novel_facts_dao.list_facts("proj-1")
    assert all(f["status"] == "active" for f in facts)


@pytest.mark.asyncio
async def test_resolve_cross_chapter_conflicts_llm_error(memory_db):
    """LLM ʧ��ʱ�������� 0�����������ɡ�"""
    novel_facts_dao.add_fact("proj-1", "completed", "�ر����ǳ¾�", "ch-1")
    new_facts = [{"fact_type": "revealed", "content": "�ر����ǳ�����"}]
    with patch.object(
        memory_service.llm, "chat", new=AsyncMock(side_effect=LLMConfigurationError("δ����"))
    ):
        count = await memory_service.resolve_cross_chapter_fact_conflicts("proj-1", new_facts)
    assert count == 0
    facts = novel_facts_dao.list_facts("proj-1")
    assert all(f["status"] == "active" for f in facts)
