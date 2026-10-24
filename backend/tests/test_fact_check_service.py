"""M3 事实核查自检闭环 —— fact_check_service 单测。

覆盖：核查上下文组装、判定解析、冲突分析、整章重写、重写编排闭环。
所有 LLM 调用均 AsyncMock 打桩（秒级完成，不等待真实 LLM）。
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import novel_facts as novel_facts_dao
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.llm.roles import ROLE_CHECK, ROLE_REASON
from app.services import fact_check_service
from app.services.fact_check_service import (
    FACT_CHECK_CATEGORIES,
    MAX_FACT_CHECK_RETRIES,
    analyze_conflict,
    build_fact_check_context,
    check_chapter,
    record_writing_conflict,
    rewrite_content,
    run_fact_check_loop,
)


# ── Fixtures ──────────────────────────────────────────

@pytest.fixture
def fact_db():
    """独立 SQLite 内存库（novel_facts 表，含 is_conflict），patch get_conn。"""
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


# 工具：构造有内容的长文本（满足重写长度校验）
def _long_text(prefix: str = "正文") -> str:
    return (prefix * 400)[:2200]


# ── build_fact_check_context ──────────────────────────

def test_build_context_with_materials():
    ctx = build_fact_check_context(
        {
            "volume_summaries": ["第一卷摘要"],
            "recent_chapter_summaries": ["第五章摘要"],
            "completed_milestones": "【已完成事实】契约签订",
        }
    )
    assert "第一卷摘要" in ctx
    assert "第五章摘要" in ctx
    assert "契约签订" in ctx


def test_build_context_empty_returns_empty():
    assert build_fact_check_context(None) == ""
    assert build_fact_check_context({}) == ""
    assert build_fact_check_context(
        {"volume_summaries": [], "recent_chapter_summaries": [], "completed_milestones": ""}
    ) == ""


def test_build_context_truncated_to_cap():
    ctx = build_fact_check_context(
        {
            "volume_summaries": ["长" * 10000],
            "recent_chapter_summaries": [],
            "completed_milestones": "",
        }
    )
    assert len(ctx) <= fact_check_service.FACT_CHECK_CONTEXT_CAP


# ── _parse_fact_check_result ──────────────────────────

def test_parse_result_pass_json():
    result = fact_check_service._parse_fact_check_result(
        '{"result": "PASS", "issues": []}'
    )
    assert result["result"] == "PASS"
    assert result["issues"] == []


def test_parse_result_fail_json_with_issues():
    reply = (
        '{"result": "FAIL", "issues": ['
        '{"category": "角色名", "detail": "张三写成李四", "fix": "统一为张三"}]}'
    )
    result = fact_check_service._parse_fact_check_result(reply)
    assert result["result"] == "FAIL"
    assert result["issues"][0]["category"] == "角色名"
    assert result["issues"][0]["detail"] == "张三写成李四"


def test_parse_result_fenced_json():
    reply = "```json\n{\"result\": \"FAIL\", \"issues\": []}\n```"
    assert fact_check_service._parse_fact_check_result(reply)["result"] == "FAIL"


def test_parse_result_bad_json_falls_back_to_text():
    assert fact_check_service._parse_fact_check_result("FAIL")["result"] == "FAIL"
    assert fact_check_service._parse_fact_check_result("PASS")["result"] == "PASS"


def test_parse_result_empty_skips():
    result = fact_check_service._parse_fact_check_result("")
    assert result["result"] == "PASS"
    assert result["skipped"] is True


def test_parse_result_garbage_defaults_pass():
    result = fact_check_service._parse_fact_check_result("完全无关的内容")
    assert result["result"] == "PASS"
    assert result["skipped"] is True


# ── check_chapter ─────────────────────────────────────

@pytest.mark.asyncio
async def test_check_chapter_pass():
    with patch.object(
        fact_check_service.llm, "chat", new=AsyncMock(return_value='{"result": "PASS", "issues": []}')
    ):
        result = await check_chapter("proj-1", "c1", _long_text())
    assert result["result"] == "PASS"
    assert result["skipped"] is False


@pytest.mark.asyncio
async def test_check_chapter_fail():
    reply = '{"result": "FAIL", "issues": [{"category": "时间线", "detail": "日期倒错", "fix": "修正"}]}'
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(return_value=reply)):
        result = await check_chapter("proj-1", "c1", _long_text())
    assert result["result"] == "FAIL"
    assert len(result["issues"]) == 1


@pytest.mark.asyncio
async def test_check_chapter_llm_error_degrades_pass():
    with patch.object(
        fact_check_service.llm, "chat", new=AsyncMock(side_effect=LLMConfigurationError("未配置"))
    ):
        result = await check_chapter("proj-1", "c1", _long_text())
    assert result["result"] == "PASS"
    assert result["skipped"] is True


@pytest.mark.asyncio
async def test_check_chapter_empty_content():
    result = await check_chapter("proj-1", "c1", "")
    assert result["result"] == "PASS"
    assert result["skipped"] is True


# ── _parse_conflict_analysis / analyze_conflict ───────

def test_parse_conflict_reconcilable():
    reply = '{"reconcilable": true, "summary": "时间线冲突", "root_cause": "事件顺序", "extra_constraints": "先A后B"}'
    result = fact_check_service._parse_conflict_analysis(reply)
    assert result["reconcilable"] is True
    assert result["extra_constraints"] == "先A后B"


def test_parse_conflict_unreconcilable_and_garbage():
    assert fact_check_service._parse_conflict_analysis('{"reconcilable": false}')["reconcilable"] is False
    assert fact_check_service._parse_conflict_analysis("乱码")["reconcilable"] is False
    assert fact_check_service._parse_conflict_analysis("")["reconcilable"] is False


@pytest.mark.asyncio
async def test_analyze_conflict_llm_error_degrades_unreconcilable():
    with patch.object(
        fact_check_service.llm, "chat", new=AsyncMock(side_effect=LLMResponseError("失败"))
    ):
        result = await analyze_conflict(_long_text(), [{"category": "设定", "detail": "矛盾"}])
    assert result["reconcilable"] is False


# ── rewrite_content ───────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_content_success():
    original = _long_text("原文")
    revised = _long_text("修订后正文")
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(return_value=revised)):
        result = await rewrite_content(original, [{"category": "角色名", "detail": "名字不一致", "fix": "统一"}])
    assert result == revised


@pytest.mark.asyncio
async def test_rewrite_content_too_short_degrades_to_original():
    original = _long_text("原文")
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(return_value="太短")):
        result = await rewrite_content(original, [{"category": "角色名", "detail": "x"}])
    assert result == original


@pytest.mark.asyncio
async def test_rewrite_content_llm_error_degrades_to_original():
    original = _long_text("原文")
    with patch.object(
        fact_check_service.llm, "chat", new=AsyncMock(side_effect=LLMConfigurationError("未配置"))
    ):
        result = await rewrite_content(original, [{"category": "角色名", "detail": "x"}])
    assert result == original


# ── run_fact_check_loop 编排 ──────────────────────────

@pytest.mark.asyncio
async def test_loop_empty_content_passes_immediately():
    result = await run_fact_check_loop("proj-1", "c1", "")
    assert result["passed"] is True
    assert result["stats"]["fact_check_count"] == 0


@pytest.mark.asyncio
async def test_loop_pass_on_first_check():
    with patch.object(
        fact_check_service.llm, "chat", new=AsyncMock(return_value='{"result": "PASS", "issues": []}')
    ):
        result = await run_fact_check_loop("proj-1", "c1", _long_text())
    assert result["passed"] is True
    assert result["stats"]["fact_check_count"] == 1
    assert result["stats"]["rewrite_count"] == 0
    assert result["conflict"] is None


@pytest.mark.asyncio
async def test_loop_fail_then_rewrite_passes(fact_db):
    """首次 FAIL → 重写 → 二次核查 PASS。"""
    revised = _long_text("修订后")
    calls = 0

    def _fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"result": "FAIL", "issues": [{"category": "角色名", "detail": "前后不一致", "fix": "统一"}]}'
        return '{"result": "PASS", "issues": []}'

    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)), \
         patch.object(fact_check_service, "rewrite_content", new=AsyncMock(return_value=revised)):
        result = await run_fact_check_loop("proj-1", "c1", _long_text("原始"))
    assert result["passed"] is True
    assert result["content"] == revised
    assert result["stats"]["fact_check_count"] == 2
    assert result["stats"]["rewrite_count"] == 1


@pytest.mark.asyncio
async def test_loop_three_fails_unreconcilable_records_conflict(fact_db):
    """3 次 FAIL 后不可调和 → 落库写作冲突（is_conflict=1），passed=False，不阻塞。"""
    revised = _long_text("修订")

    def _fake_chat(messages, **kwargs):
        system = messages[0]["content"]
        if "冲突仲裁员" in system:
            # analyze_conflict 调用
            return '{"reconcilable": false, "summary": "世界观设定矛盾", "root_cause": "两套规则冲突", "extra_constraints": ""}'
        if "小说作者" in system or "修订章节" in system:
            # rewrite_content 调用
            return revised
        # check_chapter 调用（始终 FAIL）
        return '{"result": "FAIL", "issues": [{"category": "设定", "detail": "能力前后矛盾", "fix": "修正"}]}'

    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await run_fact_check_loop(
            "proj-1", "c1", _long_text("原始"), max_retries=MAX_FACT_CHECK_RETRIES
        )

    assert result["passed"] is False
    assert result["conflict"] is not None
    # 3 次循环核查 + 1 次重写后最终核查（Codex P1-2 新增）
    assert result["stats"]["fact_check_count"] == MAX_FACT_CHECK_RETRIES + 1
    assert result["stats"]["rewrite_count"] == MAX_FACT_CHECK_RETRIES
    # 冲突已落库
    with novel_facts_dao.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM novel_facts WHERE is_conflict = 1"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["fact_type"] == "conflict"


@pytest.mark.asyncio
async def test_loop_reconcilable_records_extra_constraints_then_passes(fact_db):
    """3 次 FAIL → 可调和 → 注入约束重写 + 最终核查 PASS → passed=True。"""
    revised = _long_text("修订")
    check_calls = {"n": 0}

    def _fake_chat(messages, **kwargs):
        system = messages[0]["content"]
        if "冲突仲裁员" in system:
            return '{"reconcilable": true, "summary": "时间线", "root_cause": "顺序", "extra_constraints": "事件必须先A后B"}'
        if "小说作者" in system or "修订章节" in system:
            return revised
        # check_chapter：前 4 次 FAIL（3 次循环 + 1 次重写后最终核查），
        # 第 5 次（带约束重写后的最终核查）PASS
        check_calls["n"] += 1
        if check_calls["n"] >= 5:
            return '{"result": "PASS", "issues": []}'
        return '{"result": "FAIL", "issues": [{"category": "时间线", "detail": "顺序倒错", "fix": "调整"}]}'

    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await run_fact_check_loop(
            "proj-1", "c1", _long_text("原始"), max_retries=MAX_FACT_CHECK_RETRIES
        )

    assert result["passed"] is True
    # 3 次循环重写 + 1 次带约束重写（Codex P1-2 新增重写后最终核查不额外重写）
    assert result["stats"]["rewrite_count"] == MAX_FACT_CHECK_RETRIES + 1
    # 3 次循环核查 + 1 次重写后最终核查 + 1 次带约束后最终核查
    assert result["stats"]["fact_check_count"] == MAX_FACT_CHECK_RETRIES + 2
    assert result["conflict"] is None
    # 没有冲突落库
    with novel_facts_dao.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM novel_facts WHERE is_conflict = 1"
        ).fetchall()
    assert len(rows) == 0


# ── record_writing_conflict ───────────────────────────

def test_record_conflict_idempotent(fact_db):
    first = record_writing_conflict("proj-1", "c1", {"summary": "同一条冲突", "root_cause": "规则矛盾"})
    second = record_writing_conflict("proj-1", "c1", {"summary": "同一条冲突", "root_cause": "规则矛盾"})
    assert first is True
    assert second is False  # 同内容幂等


def test_record_conflict_empty_summary_uses_issues(fact_db):
    ok = record_writing_conflict(
        "proj-1", "c1", {}, issues=[{"category": "角色名", "detail": "名字不一致"}]
    )
    assert ok is True
    with novel_facts_dao.get_conn() as conn:
        rows = conn.execute("SELECT content FROM novel_facts WHERE is_conflict = 1").fetchall()
    assert "名字不一致" in rows[0]["content"]


# ── 常量校验 ──────────────────────────────────────────

def test_fact_check_categories_cover_five_types():
    assert set(FACT_CHECK_CATEGORIES) == {"角色名", "时间线", "地点", "已发生事件", "设定"}


# ── 第 3 档：置信度升级链 ─────────────────────────────

def test_parse_confidence_preserved_and_clamped():
    """confidence 解析保留并规范到 [0,1]；缺失默认 1.0（不触发升级）。"""
    assert fact_check_service._parse_fact_check_result(
        '{"result": "PASS", "issues": [], "confidence": 0.4}'
    )["confidence"] == 0.4
    assert fact_check_service._parse_fact_check_result(
        '{"result": "PASS", "issues": []}'
    )["confidence"] == 1.0
    assert fact_check_service._parse_fact_check_result(
        '{"result": "PASS", "issues": [], "confidence": 1.5}'
    )["confidence"] == 1.0
    assert fact_check_service._parse_fact_check_result(
        '{"result": "PASS", "issues": [], "confidence": -0.2}'
    )["confidence"] == 0.0
    assert fact_check_service._parse_fact_check_result(
        '{"result": "PASS", "issues": [], "confidence": "abc"}'
    )["confidence"] == 1.0


@pytest.mark.asyncio
async def test_check_chapter_low_confidence_escalates_to_reason():
    """低置信度（<0.6）→ 自动用 ROLE_REASON 复查一次，以强模型结果为准。"""
    calls = []

    def _fake_chat(messages, **kwargs):
        calls.append(kwargs.get("role"))
        if kwargs.get("role") == ROLE_REASON:
            return (
                '{"result": "FAIL", "issues": [{"category": "设定", '
                '"detail": "强模型发现矛盾", "fix": "修正"}], "confidence": 0.9}'
            )
        return '{"result": "PASS", "issues": [], "confidence": 0.3}'

    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await check_chapter("proj-1", "c1", _long_text())
    assert calls == [ROLE_CHECK, ROLE_REASON]
    assert result["result"] == "FAIL"  # 以强模型结果为准
    assert result["issues"][0]["category"] == "设定"
    assert result["escalated"] is True
    assert result["escalation_count"] == 1
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_check_chapter_high_confidence_no_escalation():
    """高置信度（>=0.6）→ 不升级，只调用一次。"""
    with patch.object(
        fact_check_service.llm,
        "chat",
        new=AsyncMock(return_value='{"result": "PASS", "issues": [], "confidence": 0.8}'),
    ) as mock_chat:
        result = await check_chapter("proj-1", "c1", _long_text())
    assert result["escalated"] is False
    assert result["escalation_count"] == 0
    assert mock_chat.await_count == 1


@pytest.mark.asyncio
async def test_check_chapter_escalation_failure_uses_original():
    """升级复查失败 → 降级用原结果，不抛异常。"""
    def _fake_chat(messages, **kwargs):
        if kwargs.get("role") == ROLE_REASON:
            raise LLMResponseError("强模型失败")
        return '{"result": "PASS", "issues": [], "confidence": 0.2}'

    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await check_chapter("proj-1", "c1", _long_text())
    assert result["result"] == "PASS"  # 用原结果
    assert result["escalated"] is False
    assert result["escalation_count"] == 0


@pytest.mark.asyncio
async def test_loop_stats_accumulate_escalation_count():
    """run_fact_check_loop 的 stats 累计置信度升级次数。"""
    def _fake_chat(messages, **kwargs):
        if kwargs.get("role") == ROLE_REASON:
            return '{"result": "PASS", "issues": [], "confidence": 0.9}'
        return '{"result": "PASS", "issues": [], "confidence": 0.3}'

    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await run_fact_check_loop("proj-1", "c1", _long_text())
    assert result["passed"] is True
    assert result["stats"]["fact_check_count"] == 1
    assert result["stats"]["escalation_count"] == 1
    assert result["stats"]["rewrite_count"] == 0
