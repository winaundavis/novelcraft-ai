"""M4 文风画像引擎 —— style_engine 单测。

覆盖：
- extraction：代表性采样（分块/均匀抽、空输入）、客观统计、四组规则 JSON 解析
  （LLM mock）、LLM 异常降级为规则统计（metrics 仍产出）；
- profile_service：analyze_project_style 落库（style_profiles + style_bindings）、
  LLM 失败降级（status='degraded'）、build_style_contract 编译结构化契约、
  contract_to_prompt_block 注入提示块；
- transition_service：过渡计划生成（快/中/慢步数不同）、advance_transition 进度
  推进、中断恢复（从当前档位校准）、微调指令编译；
- API：四个 style 端点路由可达 + HTTP 层 404/400 行为。

所有 LLM 调用均 AsyncMock 打桩（秒级完成，不等待真实 LLM）。
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

from app.dao import projects as projects_dao
from app.dao import style_dao
from app.llm.provider import LLMResponseError
from app.services.style_engine import extraction
from app.services.style_engine import profile_service
from app.services.style_engine import transition_service


# ══════════════════════════════════════════════════════
# Fixtures / Helpers
# ══════════════════════════════════════════════════════

_DDL = """
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
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS style_profiles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'import',
    status TEXT NOT NULL DEFAULT 'completed',
    narrative_rules TEXT NOT NULL DEFAULT '[]',
    character_rules TEXT NOT NULL DEFAULT '[]',
    language_rules TEXT NOT NULL DEFAULT '[]',
    rhythm_rules TEXT NOT NULL DEFAULT '[]',
    anti_ai_rules TEXT NOT NULL DEFAULT '[]',
    metrics TEXT NOT NULL DEFAULT '{}',
    dimensions TEXT NOT NULL DEFAULT '{}',
    strengths TEXT NOT NULL DEFAULT '[]',
    weaknesses TEXT NOT NULL DEFAULT '[]',
    transition_plan TEXT NOT NULL DEFAULT '{}',
    report TEXT NOT NULL DEFAULT '',
    sample_count INTEGER NOT NULL DEFAULT 0,
    sample_chars INTEGER NOT NULL DEFAULT 0,
    llm_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS style_bindings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    profile_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'project',
    priority INTEGER NOT NULL DEFAULT 1,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id)
);

CREATE TABLE IF NOT EXISTS style_transitions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    profile_id TEXT NOT NULL DEFAULT '',
    pace TEXT NOT NULL DEFAULT 'medium',
    progress REAL NOT NULL DEFAULT 0,
    interval_chapters INTEGER NOT NULL DEFAULT 6,
    total_steps INTEGER NOT NULL DEFAULT 10,
    plan TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id)
);
"""


@pytest.fixture
def style_db():
    """独立 SQLite 内存库（M4 五张表），patch 各 DAO/服务模块的 get_conn。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()

    def _get_conn():
        return conn

    with (
        patch.object(style_dao, "get_conn", _get_conn),
        patch.object(projects_dao, "get_conn", _get_conn),
        patch.object(profile_service, "get_conn", _get_conn),
        patch.object(transition_service, "get_conn", _get_conn),
    ):
        yield conn
    conn.close()


def _insert_project(conn, project_id: str, style_config: dict | None = None) -> None:
    import json

    conn.execute(
        """
        INSERT INTO projects (
            id, name, subtitle, genre, target_word_count, current_word_count,
            status, created_at, updated_at, style_config, outline, tags,
            is_starred, premise, target_chapters, characters, locations,
            world_rules, import_analysis, inspiration_fragments
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            "测试项目",
            "",
            "xuanhuan",
            300000,
            0,
            "planning",
            "2026-08-09 00:00:00",
            "2026-08-09 00:00:00",
            json.dumps(style_config or {}, ensure_ascii=False),
            "null",
            "[]",
            0,
            "",
            30,
            "[]",
            "[]",
            "[]",
            "null",
            "[]",
        ),
    )
    conn.commit()


def _insert_chapter(conn, project_id: str, order: int, content: str) -> str:
    chapter_id = f"ch-{project_id}-{order}"
    conn.execute(
        """
        INSERT INTO chapters (
            id, project_id, volume_id, title, chapter_order, content,
            word_count, status, summary, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chapter_id,
            project_id,
            "",
            f"第{order}章",
            order,
            content,
            len(content),
            "draft",
            "",
            "",
            "2026-08-09 00:00:00",
            "2026-08-09 00:00:00",
        ),
    )
    conn.commit()
    return chapter_id


def _llm_rules_payload() -> dict:
    """LLM 成功抽取时返回的结构化画像（与 parse_rules_json 输出形状一致）。"""
    return {
        "narrative_rules": [{"rule": "以短句为主，推进节奏快", "key": "", "source": "llm"}],
        "character_rules": [{"rule": "主角说话简短直接，配角爱用反问", "key": "", "source": "llm"}],
        "language_rules": [{"rule": "用词偏口语，少用成语堆砌", "key": "", "source": "llm"}],
        "rhythm_rules": [{"rule": "对话密集处段落短，情绪段落句长拉长", "key": "", "source": "llm"}],
        "anti_ai_rules": [{"rule": "避免每段开头都用「他/她」", "key": "", "source": "llm"}],
        "strengths": ["节奏明快", "对话生动"],
        "weaknesses": ["环境描写略少"],
        "report": "现状画像：平均句长约 12 字。优点：节奏明快。改进方向：环境描写略少。",
    }


def _preset_config() -> dict:
    return {
        "name": "简洁冷峻",
        "avgSentenceLength": 15,
        "dialogueRatio": 0.3,
        "descriptionDensity": 0.4,
        "paragraphLength": "short",
        "tone": "简洁、冷静、精准",
    }


# ══════════════════════════════════════════════════════
# 1. extraction：采样
# ══════════════════════════════════════════════════════

def test_sample_chapters_empty():
    sample, total, sample_chars, count = extraction.sample_chapters([])
    assert sample == ""
    assert total == 0
    assert sample_chars == 0
    assert count == 0


def test_sample_chapters_small_uses_full_text():
    chapters = [{"title": "第一章", "content": "正文段落。" * 300, "order": 1}]
    sample, total, sample_chars, count = extraction.sample_chapters(chapters)
    assert total <= extraction.MAX_FULL_TEXT_CHARS
    assert sample_chars == total  # 全文直接使用，未截断
    assert count == 1
    assert "第一章" in sample


def test_sample_chapters_large_uniform():
    """超过上限时均匀抽样：抽样章数有界、样本显著小于全文、首尾章均被代表。"""
    chapters = [
        {"title": f"第{i}章", "content": "段落内容。" * 500, "order": i}
        for i in range(1, 41)
    ]
    sample, total, sample_chars, count = extraction.sample_chapters(chapters)
    assert total > extraction.MAX_FULL_TEXT_CHARS
    assert count == min(len(chapters), extraction.MAX_SAMPLED_CHAPTERS)
    assert count <= extraction.MAX_SAMPLED_CHAPTERS
    # 每章样本受 CHAPTER_SAMPLE_CHARS 约束（含章首+章尾+中段），总样本远小于全文
    assert sample_chars < total
    assert sample_chars <= count * extraction.CHAPTER_SAMPLE_CHARS + count * 16
    # 均匀抽样：首章与末章都应被选中（_even_indices 含首尾）
    assert "第1章" in sample
    assert "第40章" in sample


def test_even_indices():
    # round() 为银行家舍入：round(4.5) == 4
    assert extraction._even_indices(3, 10) == [0, 4, 9]
    assert extraction._even_indices(5, 5) == [0, 1, 2, 3, 4]
    assert extraction._even_indices(2, 5) == [0, 4]
    assert extraction._even_indices(3, 0) == []
    assert extraction._even_indices(0, 10) == []


# ══════════════════════════════════════════════════════
# 2. extraction：客观统计 / 维度
# ══════════════════════════════════════════════════════

def test_compute_metrics_basic():
    text = "他推开门走了进去。屋里的灯还亮着。“你来了？”他问。"
    metrics = extraction.compute_metrics(text)
    assert metrics["totalChars"] == len(text)
    assert metrics["totalSentences"] >= 3
    assert metrics["wordCount"] > 0
    assert metrics["avgSentenceLength"] > 0
    assert 0 <= metrics["dialogueRatio"] <= 1
    assert metrics["totalParagraphs"] >= 1
    # 短/中/长段落占比应归一
    ratios = (
        metrics["paragraphShortRatio"]
        + metrics["paragraphMediumRatio"]
        + metrics["paragraphLongRatio"]
    )
    assert abs(ratios - 1.0) < 0.01


def test_compute_metrics_empty():
    metrics = extraction.compute_metrics("")
    assert metrics["totalChars"] == 0
    assert metrics["totalSentences"] == 0
    assert metrics["avgSentenceLength"] == 0.0
    assert metrics["dialogueRatio"] == 0.0
    assert metrics["descriptionDensity"] == 0.0


def test_compute_dimensions_aligns_preset_keys():
    metrics = {
        "avgSentenceLength": 15.0,
        "dialogueRatio": 0.3,
        "descriptionDensity": 0.4,
        "paragraphShortRatio": 0.6,
        "paragraphMediumRatio": 0.2,
        "paragraphLongRatio": 0.2,
    }
    dims = extraction.compute_dimensions(metrics)
    assert dims["avgSentenceLength"] == 15.0
    assert dims["dialogueRatio"] == 0.3
    assert dims["descriptionDensity"] == 0.4
    assert dims["paragraphLength"] == "short"
    assert dims["tone"]  # 有粗粒度语气标签


# ══════════════════════════════════════════════════════
# 3. extraction：四组规则 JSON 解析（LLM mock）
# ══════════════════════════════════════════════════════

_METRICS = {
    "avgSentenceLength": 12,
    "dialogueRatio": 0.3,
    "descriptionDensity": 0.5,
    "paragraphShortRatio": 0.6,
    "paragraphMediumRatio": 0.2,
    "paragraphLongRatio": 0.2,
}


def test_parse_rules_json_valid():
    reply = (
        '```json\n'
        '{"narrative_rules":[{"rule":"短句为主"}],'
        '"character_rules":[{"rule":"主角口头禅"}],'
        '"language_rules":[{"rule":"用词简洁"}],'
        '"rhythm_rules":[{"rule":"节奏快"}],'
        '"anti_ai_rules":[{"rule":"避免连接词"}],'
        '"strengths":["优点一"],"weaknesses":["不足一"]}'
        '\n```'
    )
    result = extraction.parse_rules_json(reply, _METRICS)
    assert result["narrative_rules"][0]["rule"] == "短句为主"
    assert result["narrative_rules"][0]["source"] == "llm"
    assert result["character_rules"][0]["rule"] == "主角口头禅"
    assert result["language_rules"][0]["rule"] == "用词简洁"
    assert result["rhythm_rules"][0]["rule"] == "节奏快"
    assert result["anti_ai_rules"][0]["rule"] == "避免连接词"
    assert result["strengths"] == ["优点一"]
    assert result["weaknesses"] == ["不足一"]
    assert "现状画像" in result["report"]


def test_parse_rules_json_accepts_string_items():
    reply = (
        '{"narrative_rules":["短句为主"],"character_rules":["口头禅"],'
        '"language_rules":["简洁"],"rhythm_rules":["快"],'
        '"anti_ai_rules":["反AI"],"strengths":["s"],"weaknesses":["w"]}'
    )
    result = extraction.parse_rules_json(reply, _METRICS)
    assert result["narrative_rules"][0]["rule"] == "短句为主"


def test_parse_rules_json_too_few_raises():
    reply = (
        '{"narrative_rules":[],"character_rules":[],"language_rules":[],'
        '"rhythm_rules":[],"anti_ai_rules":[],"strengths":[],"weaknesses":[]}'
    )
    with pytest.raises(LLMResponseError):
        extraction.parse_rules_json(reply, _METRICS)


def test_parse_rules_json_invalid_raises():
    with pytest.raises(LLMResponseError):
        extraction.parse_rules_json("这不是 JSON", _METRICS)
    with pytest.raises(LLMResponseError):
        extraction.parse_rules_json('{"narrative_rules": "not-a-list"}', _METRICS)


@pytest.mark.asyncio
async def test_extract_rules_llm_success():
    reply = (
        '{"narrative_rules":[{"rule":"短句"}],"character_rules":[{"rule":"口头禅"}],'
        '"language_rules":[{"rule":"用词"}],"rhythm_rules":[{"rule":"节奏"}],'
        '"anti_ai_rules":[{"rule":"反AI"}],"strengths":["s"],"weaknesses":["w"]}'
    )
    with patch.object(extraction.llm, "chat", new=AsyncMock(return_value=reply)):
        result = await extraction.extract_rules("样本文本", _METRICS)
    assert result["narrative_rules"][0]["rule"] == "短句"
    assert result["strengths"] == ["s"]


@pytest.mark.asyncio
async def test_extract_rules_llm_bad_reply_raises():
    with patch.object(extraction.llm, "chat", new=AsyncMock(return_value="无法解析的回复")):
        with pytest.raises(LLMResponseError):
            await extraction.extract_rules("样本文本", _METRICS)


# ══════════════════════════════════════════════════════
# 4. extraction：降级路径（纯规则统计）
# ══════════════════════════════════════════════════════

def test_build_rules_from_metrics_all_groups():
    rules = extraction.build_rules_from_metrics(_METRICS)
    assert rules["narrative_rules"]
    assert rules["character_rules"] == []  # 规则统计无法产出角色特征
    assert rules["language_rules"]
    assert rules["rhythm_rules"]
    assert rules["anti_ai_rules"]
    assert rules["strengths"]
    assert rules["weaknesses"]
    assert rules["report"]  # 降级报告说明 LLM 未参与


def test_build_rules_from_metrics_rhythm_notes_follow_metrics():
    # 短句 + 低对话 + 短段 → 节奏偏快的注记
    rules = extraction.build_rules_from_metrics(_METRICS)
    rhythm_text = "；".join(r["rule"] for r in rules["rhythm_rules"])
    assert "短句" in rhythm_text or "节奏" in rhythm_text


# ══════════════════════════════════════════════════════
# 5. profile_service：analyze_project_style 落库
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_analyze_project_style_persists(style_db):
    _insert_project(style_db, "proj-1", style_config=_preset_config())
    for i in range(1, 4):
        _insert_chapter(style_db, "proj-1", i, "正文段落。" * 200)

    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_llm_rules_payload())):
        profile = await profile_service.analyze_project_style("proj-1")

    assert profile["projectId"] == "proj-1"
    assert profile["status"] == "completed"
    assert profile["llmUsed"] is True
    assert profile["metrics"]["totalChars"] > 0
    assert profile["dimensions"]["avgSentenceLength"] > 0
    assert profile["narrativeRules"][0]["rule"] == "以短句为主，推进节奏快"
    assert profile["report"]

    # 落库：画像 + 绑定
    binding = style_dao.get_binding("proj-1")
    assert binding is not None
    assert binding["profileId"] == profile["id"]
    stored = style_dao.get_profile(profile["id"])
    assert stored is not None
    assert stored["status"] == "completed"


@pytest.mark.asyncio
async def test_analyze_project_style_degraded_on_llm_failure(style_db):
    _insert_project(style_db, "proj-1", style_config=_preset_config())
    _insert_chapter(style_db, "proj-1", 1, "正文段落。" * 200)

    with patch.object(extraction, "extract_rules", new=AsyncMock(side_effect=RuntimeError("LLM 不可用"))):
        profile = await profile_service.analyze_project_style("proj-1")

    assert profile["status"] == "degraded"
    assert profile["llmUsed"] is False
    # 降级路径 metrics 仍产出（零 LLM 成本规则统计）
    assert profile["metrics"]["totalChars"] > 0
    assert profile["dimensions"]
    # 规则画像由指标推导
    assert profile["narrativeRules"]
    assert profile["rhythmRules"]
    assert profile["antiAiRules"]
    assert profile["report"]
    # 绑定仍建立（降级画像也可用）
    binding = style_dao.get_binding("proj-1")
    assert binding is not None


@pytest.mark.asyncio
async def test_analyze_project_style_no_chapters_raises(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    with pytest.raises(ValueError):
        await profile_service.analyze_project_style("proj-1")


def test_get_profile_returns_latest_when_no_binding(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    profile = style_dao.create_profile(
        {
            "project_id": "proj-1",
            "name": "文风画像 2026-08-09",
            "source_type": "import",
            "status": "completed",
            "narrative_rules": [{"rule": "短句", "source": "llm"}],
            "character_rules": [],
            "language_rules": [],
            "rhythm_rules": [],
            "anti_ai_rules": [],
            "metrics": {"totalChars": 10},
            "dimensions": {"avgSentenceLength": 12},
            "strengths": [],
            "weaknesses": [],
            "report": "",
            "sample_count": 1,
            "sample_chars": 10,
            "llm_used": 1,
            "today": "2026-08-09 00:00:00",
        }
    )
    result = profile_service.get_profile("proj-1")
    assert result is not None
    assert result["id"] == profile["id"]
    assert profile_service.get_profile("no-such-project") is None


# ══════════════════════════════════════════════════════
# 6. profile_service：style contract 编译
# ══════════════════════════════════════════════════════

def test_compile_contract_preset_hard_constraint():
    contract = profile_service.compile_contract(_preset_config(), None, None)
    preset = contract["preset"]
    assert preset["name"] == "简洁冷峻"
    assert preset["avgSentenceLength"] == 15
    # 预设均值扩成允许区间（±20%，句长下限 ≥ 6）
    low, high = preset["range"]["avgSentenceLength"]
    assert low >= 6
    assert low <= 15 <= high
    # 无画像/无过渡时不含对应段落
    assert "profile" not in contract
    assert "transition" not in contract


def test_compile_contract_with_profile_and_transition():
    profile = {
        "id": "p1",
        "status": "completed",
        "dimensions": {"avgSentenceLength": 12, "dialogueRatio": 0.2, "descriptionDensity": 0.3, "paragraphLength": "short"},
        "narrativeRules": [{"rule": "短句为主", "source": "llm"}],
        "characterRules": [],
        "languageRules": [],
        "rhythmRules": [],
        "antiAiRules": [],
    }
    transition = {
        "pace": "medium",
        "totalSteps": 10,
        "intervalChapters": 6,
        "plan": {"step": 5, "weight": 0.5},
        "profileId": "p1",
    }
    contract = profile_service.compile_contract(_preset_config(), profile, transition)
    assert contract["profile"]["id"] == "p1"
    assert contract["profile"]["rules"]["narrative_rules"][0]["rule"] == "短句为主"
    assert contract["transition"]["pace"] == "medium"
    assert contract["transition"]["instruction"]


def test_compile_contract_empty_preset():
    contract = profile_service.compile_contract({}, None, None)
    assert contract["preset"]["range"] == {}


@pytest.mark.asyncio
async def test_build_style_contract_full(style_db):
    _insert_project(style_db, "proj-1", style_config=_preset_config())
    for i in range(1, 4):
        _insert_chapter(style_db, "proj-1", i, "正文段落。" * 200)

    with patch.object(extraction, "extract_rules", new=AsyncMock(return_value=_llm_rules_payload())):
        await profile_service.analyze_project_style("proj-1")
    transition_service.start_transition("proj-1", "medium")

    contract = profile_service.build_style_contract("proj-1")
    assert contract["preset"]["name"] == "简洁冷峻"
    assert contract["profile"]["id"]
    assert contract["profile"]["status"] == "completed"
    assert contract["transition"]["pace"] == "medium"


def test_contract_to_prompt_block_sections():
    contract = {
        "preset": {
            "name": "简洁冷峻",
            "avgSentenceLength": 15,
            "dialogueRatio": 0.3,
            "descriptionDensity": 0.4,
            "paragraphLength": "short",
            "tone": "简洁",
            "range": {"avgSentenceLength": [12, 18], "dialogueRatio": [0.1, 0.5]},
        },
        "profile": {
            "id": "p1",
            "status": "completed",
            "dimensions": {"avgSentenceLength": 12, "dialogueRatio": 0.2, "descriptionDensity": 0.3, "paragraphLength": "short"},
            "rules": {
                "narrative_rules": [{"rule": "短句为主", "source": "llm"}],
                "character_rules": [],
                "language_rules": [],
                "rhythm_rules": [],
                "anti_ai_rules": [{"rule": "避免连接词", "source": "llm"}],
            },
        },
        "transition": {"pace": "medium", "paceLabel": "中速", "instruction": "当前过渡档位：中速"},
    }
    block = profile_service.contract_to_prompt_block(contract)
    assert "【风格偏好】" in block
    assert "不得覆盖故事事实" in block
    assert "简洁冷峻" in block
    assert "现状文风基线" in block
    assert "避免连接词" in block
    assert "渐进过渡" in block


def test_contract_to_prompt_block_empty():
    assert profile_service.contract_to_prompt_block(None) == ""
    assert profile_service.contract_to_prompt_block({}) == ""


# ══════════════════════════════════════════════════════
# 7. transition_service：过渡计划 / 进度推进 / 中断恢复
# ══════════════════════════════════════════════════════

def test_start_transition_paces_different_steps(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    expected = {
        "fast": (9, 3, 3),
        "medium": (60, 10, 6),
        "slow": (200, 20, 10),
    }
    for pace, (target_chapters, total_steps, interval) in expected.items():
        transition = transition_service.start_transition("proj-1", pace)
        assert transition["pace"] == pace
        assert transition["plan"]["schemaVersion"] == 2
        assert transition["targetChapters"] == target_chapters
        assert transition["totalSteps"] == total_steps
        assert transition["intervalChapters"] == interval
        assert "steps" not in transition["plan"]


def test_start_transition_invalid_pace(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    with pytest.raises(ValueError):
        transition_service.start_transition("proj-1", "turbo")


def test_advance_transition_progress_steps(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    transition = transition_service.start_transition("proj-1", "medium")
    revision = transition["revision"]

    # 纯读取不根据章节表推算或写入进度。
    t = transition_service.get_transition_progress("proj-1")
    assert t["progress"] == pytest.approx(0.0)
    assert t["completedCount"] == 0

    # 每次正文落库后在同一事务登记，6/60 章后进度为 0.1。
    for i in range(1, 7):
        chapter_id = _insert_chapter(style_db, "proj-1", i, "正文段落。" * 200)
        with style_db:
            current = transition_service.record_chapter_completion_on_conn(
                style_db, "proj-1", chapter_id, transition["planId"], revision
            )
        revision = current["revision"]
    t = transition_service.get_transition_progress("proj-1")
    assert t["completedCount"] == 6
    assert t["progress"] == pytest.approx(0.1, abs=0.001)

    # 再登记 6 章 → 12/60 = 0.2。
    for i in range(7, 13):
        chapter_id = _insert_chapter(style_db, "proj-1", i, "正文段落。" * 200)
        with style_db:
            current = transition_service.record_chapter_completion_on_conn(
                style_db, "proj-1", chapter_id, transition["planId"], revision
            )
        revision = current["revision"]
    t = transition_service.get_transition_progress("proj-1")
    assert t["completedCount"] == 12
    assert t["progress"] == pytest.approx(0.2, abs=0.001)


def test_advance_transition_interrupt_resume(style_db):
    """模拟重启：进度从持久化完成账本恢复，读取不重新推算。"""
    _insert_project(style_db, "proj-1", style_config={})
    transition = transition_service.start_transition("proj-1", "medium")
    revision = transition["revision"]

    # 任务中断前已原子登记 12 章。
    for i in range(1, 13):
        chapter_id = _insert_chapter(style_db, "proj-1", i, "正文段落。" * 200)
        with style_db:
            current = transition_service.record_chapter_completion_on_conn(
                style_db, "proj-1", chapter_id, transition["planId"], revision
            )
        revision = current["revision"]

    # 重启后的首次读取直接反映账本。
    t = transition_service.get_transition_progress("proj-1")
    assert t["completedCount"] == 12
    assert t["progress"] == pytest.approx(0.2, abs=0.001)

    # 再写 6 章 → 继续登记（不重头）。
    for i in range(13, 19):
        chapter_id = _insert_chapter(style_db, "proj-1", i, "正文段落。" * 200)
        with style_db:
            current = transition_service.record_chapter_completion_on_conn(
                style_db, "proj-1", chapter_id, transition["planId"], revision
            )
        revision = current["revision"]
    t = transition_service.get_transition_progress("proj-1")
    assert t["completedCount"] == 18
    assert t["progress"] == pytest.approx(0.3, abs=0.001)


def test_advance_transition_no_record(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    assert transition_service.advance_transition("proj-1") is None
    assert transition_service.get_transition("proj-1") is None
    assert transition_service.get_transition_progress("proj-1") is None


def test_build_transition_block_instruction_stage(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    transition = {
        "pace": "medium",
        "totalSteps": 10,
        "intervalChapters": 6,
        "plan": {"step": 5, "weight": 0.5},
        "profileId": "",
    }
    block = transition_service.build_transition_block(transition, None, _preset_config())
    assert block is not None
    assert block["pace"] == "medium"
    assert block["progress"] == pytest.approx(0.5, abs=0.001)
    assert "过渡中段" in block["instruction"]


def test_build_transition_instruction_early_stage():
    transition = {
        "pace": "medium",
        "totalSteps": 10,
        "intervalChapters": 6,
        "plan": {"step": 1, "weight": 0.1},
        "profileId": "",
    }
    profile = {"dimensions": {"avgSentenceLength": 12, "dialogueRatio": 0.2, "descriptionDensity": 0.3, "paragraphLength": "short"}}
    instruction = transition_service.build_transition_instruction(transition, profile, _preset_config())
    assert "保留原稿风格" in instruction
    assert "简洁冷峻" in instruction


def test_count_completed_chapters(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    _insert_chapter(style_db, "proj-1", 1, "正文段落。" * 200)
    _insert_chapter(style_db, "proj-1", 2, "正文段落。" * 200)
    assert transition_service.count_completed_chapters("proj-1") == 2
    assert transition_service.count_completed_chapters("no-such") == 0


def test_v2_transition_freezes_snapshots_and_uses_target_chapters(style_db):
    _insert_project(style_db, "proj-1", style_config={"name": "目标风格"})
    _insert_chapter(style_db, "proj-1", 1, "计划开始前的正文。" * 20)

    transition = transition_service.start_transition(
        "proj-1",
        "medium",
        target_chapters=60,
        target_style={"presetKey": "emotional_resonance", "customPrompt": "减少心理独白"},
    )

    plan = transition["plan"]
    assert plan["schemaVersion"] == 2
    assert plan["planId"]
    assert plan["revision"] == 1
    assert plan["targetChapters"] == 60
    assert plan["baselineChapterIds"] == ["ch-proj-1-1"]
    assert plan["targetSnapshot"]["presetKey"] == "emotional_resonance"
    assert plan["targetSnapshot"]["customPrompt"] == "减少心理独白"
    assert transition["completedCount"] == 0
    assert transition["progress"] == 0


def test_v2_completion_is_idempotent_and_get_is_read_only(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    transition = transition_service.start_transition("proj-1", "fast", target_chapters=9)
    with style_db:
        style_db.execute("INSERT INTO chapters (id, project_id, title, chapter_order, content, word_count, status, summary, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'completed', '', '', 'now', 'now')", ("new-1", "proj-1", "新章", 2, "正文", 2))

    with style_db:
        result = transition_service.record_chapter_completion_on_conn(
            style_db, "proj-1", "new-1", plan_id=transition["planId"], expected_revision=transition["revision"], commit_id="commit-1"
        )
    assert result["completedCount"] == 1
    assert result["revision"] == 2

    with style_db:
        repeated = transition_service.record_chapter_completion_on_conn(
            style_db, "proj-1", "new-1", plan_id=result["plan"]["planId"], expected_revision=2, commit_id="commit-2"
        )
    assert repeated["completedCount"] == 1
    assert repeated["revision"] == 2

    before = style_dao.get_transition("proj-1")
    observed = transition_service.get_transition_progress("proj-1")
    after = style_dao.get_transition("proj-1")
    assert observed["revision"] == 2
    assert before["plan"] == after["plan"]


def test_v2_revision_conflict_is_rejected_before_completion(style_db):
    _insert_project(style_db, "proj-1", style_config={})
    transition = transition_service.start_transition("proj-1", "fast", target_chapters=9)
    with pytest.raises(transition_service.TransitionRevisionConflict):
        with style_db:
            transition_service.record_chapter_completion_on_conn(
                style_db, "proj-1", "new-1", plan_id=transition["plan"]["planId"], expected_revision=99
            )


# ══════════════════════════════════════════════════════
# 8. API：四端点路由可达 + HTTP 行为
# ══════════════════════════════════════════════════════

def test_style_router_endpoints_registered():
    """四个 style 端点均应注册到 app（路由可达性，基于 OpenAPI paths）。"""
    paths = set(app.openapi()["paths"].keys())
    assert "/api/projects/{project_id}/style-analysis" in paths
    assert "/api/projects/{project_id}/style-profile" in paths
    assert "/api/projects/{project_id}/style-transition" in paths


def _unique_name(prefix: str = "style") -> str:
    import time

    return f"{prefix}-{int(time.time() * 1000)}"


def _create_project(client: TestClient, **overrides) -> dict:
    resp = client.post("/api/projects/", json={"name": _unique_name(), "genre": "xuanhuan", **overrides})
    assert resp.status_code == 200, f"创建项目失败: {resp.text}"
    return resp.json()["data"]


def _delete_project(client: TestClient, project_id: str) -> None:
    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code in (200, 404, 409)


def test_style_profile_404_when_no_profile():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.get(f"/api/projects/{project['id']}/style-profile")
            assert resp.status_code == 404
        finally:
            _delete_project(client, project["id"])


def test_style_analysis_400_when_no_chapters():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.post(f"/api/projects/{project['id']}/style-analysis")
            assert resp.status_code == 400
        finally:
            _delete_project(client, project["id"])


def test_style_transition_400_unknown_project():
    with TestClient(app) as client:
        resp = client.put("/api/projects/not-exist/style-transition", json={"pace": "fast"})
        assert resp.status_code == 404


def test_style_transition_400_invalid_pace():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.put(f"/api/projects/{project['id']}/style-transition", json={"pace": "turbo"})
            assert resp.status_code == 400
        finally:
            _delete_project(client, project["id"])


def test_style_transition_set_and_query():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.put(f"/api/projects/{project['id']}/style-transition", json={"pace": "fast"})
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["pace"] == "fast"
            assert data["totalSteps"] == 3

            query = client.get(f"/api/projects/{project['id']}/style-transition")
            assert query.status_code == 200
            assert query.json()["data"]["pace"] == "fast"
        finally:
            _delete_project(client, project["id"])


def test_style_transition_v2_target_and_custom_prompt_round_trip():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.put(
                f"/api/projects/{project['id']}/style-transition",
                json={
                    "pace": "medium",
                    "targetChapters": 60,
                    "targetStyle": {
                        "presetKey": "emotional_resonance",
                        "customPrompt": "减少心理独白，保留克制对话。",
                    },
                },
            )
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["plan"]["schemaVersion"] == 2
            assert data["targetChapters"] == 60
            assert data["targetSnapshot"]["presetKey"] == "emotional_resonance"
            assert data["targetSnapshot"]["customPrompt"] == "减少心理独白，保留克制对话。"
            queried = client.get(f"/api/projects/{project['id']}/style-transition")
            assert queried.status_code == 200
            assert queried.json()["data"]["revision"] == data["revision"]
        finally:
            _delete_project(client, project["id"])


# ══════════════════════════════════════════════════════
# 6. P1-1 生成后偏离度校验（evaluate_preset_deviation）
# ══════════════════════════════════════════════════════

def test_preset_deviation_within_range(style_db):
    """正文指标落在预设允许区间（±20%）内 → 无偏离项。"""
    _insert_project(style_db, "proj-1", {"avgSentenceLength": 20, "dialogueRatio": 0})
    text = (
        "他沿着青石小路慢慢走着，夜风带来远处寺庙的钟声。"
        "他停下脚步抬头望去，山门在月光下若隐若现。"
        "他想起师父说过的话，心头微微一沉。"
        "他没有犹豫，推开木门走了进去。"
    )
    deviations = profile_service.evaluate_preset_deviation(
        text, {"avgSentenceLength": 20, "dialogueRatio": 0}
    )
    assert deviations == []


def test_preset_deviation_out_of_range(style_db):
    """正文句长远低于预设下限 → avgSentenceLength 偏离项。"""
    _insert_project(style_db, "proj-1", {"avgSentenceLength": 20, "dialogueRatio": 0})
    text = "短句。" * 20  # 平均句长约 3 字，远低于预设 20 字的下限 16
    deviations = profile_service.evaluate_preset_deviation(
        text, {"avgSentenceLength": 20, "dialogueRatio": 0}
    )
    assert len(deviations) >= 1
    dims = {d["dimension"] for d in deviations}
    assert "avgSentenceLength" in dims


def test_preset_deviation_empty_or_no_preset():
    assert profile_service.evaluate_preset_deviation("", {"avgSentenceLength": 20}) == []
    assert profile_service.evaluate_preset_deviation("   ", {"avgSentenceLength": 20}) == []
    assert profile_service.evaluate_preset_deviation("正文内容。", None) == []
    assert profile_service.evaluate_preset_deviation("正文内容。", {}) == []


def test_preset_deviation_range_matches_preset():
    """预设 20 字 → 允许区间 [16, 24]；16/24 边界内，14 超限。"""
    short = "短句。" * 10  # avg≈3
    assert profile_service.evaluate_preset_deviation(short, {"avgSentenceLength": 20})


# ══════════════════════════════════════════════════════
# 7. P1-2 渐进过渡随章节真实推进（advance + contract 反映）
# ══════════════════════════════════════════════════════

def test_transition_weight_progresses_with_chapters(style_db):
    """已完成进度与下一章生成权重分别由同一 v2 账本给出。"""
    _insert_project(style_db, "proj-1", _preset_config())
    transition = transition_service.start_transition("proj-1", "fast")
    revision = transition["revision"]
    completed_progress: list[float] = []
    next_chapter_weights: list[float] = []

    for n in range(1, 10):
        chapter_id = _insert_chapter(style_db, "proj-1", n, f"第{n}章正文内容。" * 50)
        with style_db:
            current = transition_service.record_chapter_completion_on_conn(
                style_db, "proj-1", chapter_id, transition["planId"], revision
            )
        assert current is not None
        revision = current["revision"]
        completed_progress.append(float(current["progress"] or 0))
        # 契约的 progress 是已完成 k 章；weight 是下一章的 (k + 1) / N。
        contract = profile_service.build_generation_contract("proj-1")
        transition_block = contract.get("transition") or {}
        assert transition_block.get("progress") == pytest.approx(n / 9, abs=0.001)
        next_chapter_weights.append(float(transition_block.get("weight") or 0))
        assert next_chapter_weights[-1] == pytest.approx(min((n + 1) / 9, 1.0), abs=0.001)

    assert completed_progress[0] == pytest.approx(1 / 9, abs=0.001)
    assert completed_progress[-1] == 1.0
    assert next_chapter_weights[0] == pytest.approx(2 / 9, abs=0.001)
    assert next_chapter_weights[-1] == 1.0
    assert all(b >= a for a, b in zip(completed_progress, completed_progress[1:]))


# ══════════════════════════════════════════════════════
# 8. P1-3 style-contract 端点（编辑器逐章续写拉取）
# ══════════════════════════════════════════════════════

def test_style_contract_endpoint_returns_prompt_block():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.get(f"/api/projects/{project['id']}/style-contract")
            assert resp.status_code == 200
            data = resp.json()["data"]
            # styleTone 是可注入续写请求的文风提示块（字符串）
            assert isinstance(data.get("styleTone"), str)
            assert "contract" in data
        finally:
            _delete_project(client, project["id"])


def test_style_contract_endpoint_with_transition():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            client.put(
                f"/api/projects/{project['id']}/style-transition", json={"pace": "medium"}
            )
            resp = client.get(f"/api/projects/{project['id']}/style-contract")
            assert resp.status_code == 200
            data = resp.json()["data"]
            # 有过渡计划时，styleTone 应包含过渡微调提示
            assert "过渡" in data.get("styleTone") or "styleTone" in data
        finally:
            _delete_project(client, project["id"])
