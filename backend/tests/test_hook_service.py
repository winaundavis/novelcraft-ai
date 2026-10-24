"""M3 钩子检测（强钩子管理）—— hook_service 单测。

覆盖：钩子指令生成、判定解析、结尾重写、落库、上一章钩子状态迁移。
所有 LLM 调用均 AsyncMock 打桩（秒级完成）。
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import chapter_hooks as chapter_hooks_dao
from app.dao.chapter_hooks import (
    HOOK_STATUS_CARRIED,
    HOOK_STATUS_PLANTED,
    HOOK_STATUS_RESOLVED,
)
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.llm.roles import ROLE_CHECK, ROLE_REASON
from app.services import hook_service
from app.services.hook_service import (
    build_hook_instruction,
    check_hook,
    record_hook,
    rewrite_ending,
    update_previous_hooks,
)


# ── Fixtures ──────────────────────────────────────────

@pytest.fixture
def hook_db():
    """独立 SQLite 内存库（chapter_hooks 表），patch get_conn。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chapter_hooks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL DEFAULT '',
            hook_type TEXT NOT NULL DEFAULT 'suspense',
            status TEXT NOT NULL DEFAULT 'planted',
            target_chapter TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, chapter_id)
        );
        """
    )
    conn.commit()

    def _get_conn():
        return conn

    with patch.object(chapter_hooks_dao, "get_conn", _get_conn):
        yield conn
    conn.close()


def _long_text(prefix: str = "正文") -> str:
    return (prefix * 400)[:2200]


# ── build_hook_instruction ────────────────────────────

def test_build_instruction_with_open_hooks():
    instr = build_hook_instruction(
        [
            {"hook_type": "suspense", "status": "planted", "description": "密室里的钥匙"},
            {"hook_type": "twist", "status": "carried", "description": "身份之谜"},
        ],
        "第十章",
    )
    assert "密室里的钥匙" in instr
    assert "身份之谜" in instr
    assert "承接" in instr


def test_build_instruction_without_open_hooks():
    instr = build_hook_instruction([], "第一章")
    assert "强钩子" in instr
    assert "悬念" in instr  # 6 种类型之一被提及


def test_build_instruction_filters_non_open():
    instr = build_hook_instruction(
        [
            {"hook_type": "suspense", "status": "resolved", "description": "已回收的"},
            {"hook_type": "crisis", "status": "planted", "description": "危机逼近"},
        ],
        "第十一章",
    )
    assert "已回收的" not in instr
    assert "危机逼近" in instr


# ── _parse_hook_check ─────────────────────────────────

def test_parse_hook_true_json():
    result = hook_service._parse_hook_check(
        '{"has_hook": true, "hook_type": "twist", "description": "信物是假的"}'
    )
    assert result["has_hook"] is True
    assert result["hook_type"] == "twist"
    assert result["description"] == "信物是假的"


def test_parse_hook_false_json():
    result = hook_service._parse_hook_check('{"has_hook": false, "hook_type": null, "description": "平淡收尾"}')
    assert result["has_hook"] is False


def test_parse_hook_fenced_and_garbage():
    assert hook_service._parse_hook_check('```json\n{"has_hook": true}\n```')["has_hook"] is True
    assert hook_service._parse_hook_check("无钩")["has_hook"] is False
    assert hook_service._parse_hook_check("")["has_hook"] is True
    assert hook_service._parse_hook_check("无关内容")["has_hook"] is True


def test_parse_hook_invalid_type_normalized_to_none():
    result = hook_service._parse_hook_check('{"has_hook": true, "hook_type": "weird"}')
    assert result["hook_type"] is None


# ── check_hook ────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_hook_true():
    with patch.object(
        hook_service.llm, "chat",
        new=AsyncMock(return_value='{"has_hook": true, "hook_type": "crisis", "description": "追兵将至"}'),
    ):
        result = await check_hook(_long_text())
    assert result["has_hook"] is True
    assert result["hook_type"] == "crisis"


@pytest.mark.asyncio
async def test_check_hook_false():
    with patch.object(
        hook_service.llm, "chat", new=AsyncMock(return_value='{"has_hook": false}')
    ):
        result = await check_hook(_long_text())
    assert result["has_hook"] is False


@pytest.mark.asyncio
async def test_check_hook_llm_error_degrades_true():
    with patch.object(
        hook_service.llm, "chat", new=AsyncMock(side_effect=LLMConfigurationError("未配置"))
    ):
        result = await check_hook(_long_text())
    assert result["has_hook"] is True
    assert result["skipped"] is True


@pytest.mark.asyncio
async def test_check_hook_empty_content():
    result = await check_hook("")
    assert result["has_hook"] is True


# ── rewrite_ending ────────────────────────────────────

@pytest.mark.asyncio
async def test_rewrite_ending_success_replaces_tail():
    original = _long_text("前文")
    new_ending = "他猛地推开门，却发现门外站着的人正是本该死去的陈墨！" * 4  # ≥80 字
    with patch.object(hook_service.llm, "chat", new=AsyncMock(return_value=new_ending)):
        result = await rewrite_ending(original)
    assert result != original
    assert new_ending.strip() in result
    # 前文保留
    assert original[:200] in result


@pytest.mark.asyncio
async def test_rewrite_ending_too_short_degrades():
    original = _long_text("前文")
    with patch.object(hook_service.llm, "chat", new=AsyncMock(return_value="太短")):
        result = await rewrite_ending(original)
    assert result == original


@pytest.mark.asyncio
async def test_rewrite_ending_llm_error_degrades():
    original = _long_text("前文")
    with patch.object(
        hook_service.llm, "chat", new=AsyncMock(side_effect=LLMResponseError("失败"))
    ):
        result = await rewrite_ending(original)
    assert result == original


# ── record_hook ───────────────────────────────────────

def test_record_hook_plants(hook_db):
    ok = record_hook(
        "proj-1", "c1",
        {"has_hook": True, "hook_type": "suspense", "description": "钥匙之谜"},
        target_chapter="c2",
    )
    assert ok is True
    hook = chapter_hooks_dao.get_hook("proj-1", "c1")
    assert hook["status"] == HOOK_STATUS_PLANTED
    assert hook["hook_type"] == "suspense"
    assert hook["target_chapter"] == "c2"


def test_record_hook_no_hook_skips(hook_db):
    ok = record_hook("proj-1", "c1", {"has_hook": False, "hook_type": None, "description": ""})
    assert ok is False
    assert chapter_hooks_dao.count_hooks("proj-1") == 0


def test_record_hook_upsert_idempotent(hook_db):
    record_hook("proj-1", "c1", {"has_hook": True, "hook_type": "twist", "description": "身份"})
    record_hook("proj-1", "c1", {"has_hook": True, "hook_type": "crisis", "description": "追兵"})
    assert chapter_hooks_dao.count_hooks("proj-1") == 1  # 同章只一条
    hook = chapter_hooks_dao.get_hook("proj-1", "c1")
    assert hook["hook_type"] == "crisis"  # 后写覆盖


# ── update_previous_hooks 状态迁移 ────────────────────

@pytest.mark.asyncio
async def test_update_previous_hook_planted_to_resolved(hook_db):
    record_hook("proj-1", "c1", {"has_hook": True, "hook_type": "suspense", "description": "谜"})
    with patch.object(hook_service.llm, "chat", new=AsyncMock(return_value='{"resolved": true}')):
        await update_previous_hooks("proj-1", "c1", _long_text("承接"))
    assert chapter_hooks_dao.get_hook("proj-1", "c1")["status"] == HOOK_STATUS_RESOLVED


@pytest.mark.asyncio
async def test_update_previous_hook_planted_to_carried(hook_db):
    record_hook("proj-1", "c1", {"has_hook": True, "hook_type": "suspense", "description": "谜"})
    with patch.object(hook_service.llm, "chat", new=AsyncMock(return_value='{"resolved": false}')):
        await update_previous_hooks("proj-1", "c1", _long_text("未解决"))
    assert chapter_hooks_dao.get_hook("proj-1", "c1")["status"] == HOOK_STATUS_CARRIED


@pytest.mark.asyncio
async def test_update_previous_hook_llm_error_degrades_carried(hook_db):
    record_hook("proj-1", "c1", {"has_hook": True, "hook_type": "suspense", "description": "谜"})
    with patch.object(
        hook_service.llm, "chat", new=AsyncMock(side_effect=LLMConfigurationError("未配置"))
    ):
        await update_previous_hooks("proj-1", "c1", _long_text("内容"))
    assert chapter_hooks_dao.get_hook("proj-1", "c1")["status"] == HOOK_STATUS_CARRIED


@pytest.mark.asyncio
async def test_update_previous_hook_no_hook_noop(hook_db):
    await update_previous_hooks("proj-1", "c-none", _long_text("内容"))
    assert chapter_hooks_dao.count_hooks("proj-1") == 0


@pytest.mark.asyncio
async def test_update_previous_hook_resolved_stays(hook_db):
    chapter_hooks_dao.upsert_hook(
        "proj-1", "c1", hook_type="suspense", status=HOOK_STATUS_RESOLVED, description="已回收"
    )
    with patch.object(hook_service.llm, "chat", new=AsyncMock(return_value='{"resolved": true}')):
        await update_previous_hooks("proj-1", "c1", _long_text("内容"))
    assert chapter_hooks_dao.get_hook("proj-1", "c1")["status"] == HOOK_STATUS_RESOLVED


# ── 第 3 档：置信度升级链 ─────────────────────────────

def test_parse_hook_confidence_preserved():
    """钩子判定的 confidence 解析保留；缺失默认 1.0。"""
    assert hook_service._parse_hook_check('{"has_hook": true, "confidence": 0.5}')["confidence"] == 0.5
    assert hook_service._parse_hook_check('{"has_hook": true}')["confidence"] == 1.0
    assert hook_service._parse_hook_check("")["confidence"] == 1.0
    assert hook_service._parse_hook_check('{"has_hook": false, "confidence": 1.2}')["confidence"] == 1.0


@pytest.mark.asyncio
async def test_check_hook_low_confidence_escalates_to_reason():
    """低置信度 → 自动用 ROLE_REASON 复查一次，以强模型结果为准。"""
    calls = []

    def _fake_chat(messages, **kwargs):
        calls.append(kwargs.get("role"))
        if kwargs.get("role") == ROLE_REASON:
            return '{"has_hook": false, "hook_type": null, "description": "强模型：平淡收尾", "confidence": 0.9}'
        return '{"has_hook": true, "hook_type": "suspense", "description": "存疑", "confidence": 0.2}'

    with patch.object(hook_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await check_hook(_long_text())
    assert calls == [ROLE_CHECK, ROLE_REASON]
    assert result["has_hook"] is False  # 以强模型结果为准
    assert result["escalated"] is True
    assert result["escalation_count"] == 1
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_check_hook_high_confidence_no_escalation():
    """高置信度（>=0.6）→ 不升级，只调用一次。"""
    with patch.object(
        hook_service.llm,
        "chat",
        new=AsyncMock(return_value='{"has_hook": true, "confidence": 0.9}'),
    ) as mock_chat:
        result = await check_hook(_long_text())
    assert result["escalated"] is False
    assert result["escalation_count"] == 0
    assert mock_chat.await_count == 1


@pytest.mark.asyncio
async def test_check_hook_escalation_failure_uses_original():
    """升级复查失败 → 降级用原结果，不抛异常。"""
    def _fake_chat(messages, **kwargs):
        if kwargs.get("role") == ROLE_REASON:
            raise LLMResponseError("强模型失败")
        return '{"has_hook": true, "confidence": 0.2}'

    with patch.object(hook_service.llm, "chat", new=AsyncMock(side_effect=_fake_chat)):
        result = await check_hook(_long_text())
    assert result["has_hook"] is True  # 用原结果
    assert result["escalated"] is False
    assert result["escalation_count"] == 0
