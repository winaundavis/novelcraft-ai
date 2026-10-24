"""文笔局部重写服务（rewrite_service）单元测试 —— Codex 方案执行顺序第 8 步。

覆盖：
- chapter_rewrites DAO：写入/查询/更新状态/计数/非法状态归一（SQLite 内存库）
- prompt_contract：8 级优先级 + D1-D10 生成侧指令 + ai_service._build_continue_prompt 注入
- rewrite_service：
  - heuristic_rubric_scores：10 维 + 加权总分（量表 6.1 公式）
  - 5 种重写类型分发（_do_rewrite）：开头/对话/行动/情绪/结尾
  - 审计痕迹：每次重写落表（原文/原因/结果/评分前后/状态）
  - 自动回退：事实核查 FAIL → reverted；综合评分下降 → reverted；结尾仍无钩 → reverted
  - 「必须人工」维度只记债务不自动重写；第一章开篇只记债务
  - LLM 失败降级：返回原文，不抛异常
  - run_style_rewrite_chain 编排统计
  - apply_rewrite：应用重写写 chapters.content；reverted 记录拒绝应用
- API：GET /api/rewrites / POST /api/rewrites/run / POST /api/rewrites/{id}/apply
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

from app.dao import chapter_rewrites as chapter_rewrites_dao
from app.services import rewrite_service
from app.services.prompt_contract import (
    RUBRIC_TO_GENERATION_INSTRUCTIONS,
    build_full_style_contract_block,
    build_priority_block,
    build_rubric_generation_instructions,
)
from app.services.rewrite_service import (
    REWRITE_TYPE_ACTION,
    REWRITE_TYPE_DIALOGUE,
    REWRITE_TYPE_EMOTION,
    REWRITE_TYPE_ENDING,
    REWRITE_TYPE_OPENING,
    heuristic_rubric_scores,
)


# ══════════════════════════════════════════════════════
# 内存库 fixture（chapter_rewrites + chapters）
# ══════════════════════════════════════════════════════

_CHAPTER_REWRITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS chapter_rewrites (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL DEFAULT '',
    rewrite_type TEXT NOT NULL DEFAULT 'opening',
    original TEXT NOT NULL DEFAULT '',
    rewritten TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    score_before REAL NOT NULL DEFAULT 0,
    score_after REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL DEFAULT ''
);
"""

_CHAPTERS_SCHEMA = """
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
"""


@pytest.fixture
def rewrite_db():
    """独立 SQLite 内存库，patch DAO 与服务的 get_conn。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_CHAPTER_REWRITES_SCHEMA)
    conn.executescript(_CHAPTERS_SCHEMA)
    conn.commit()

    def _get_conn():
        return conn

    with (
        patch.object(chapter_rewrites_dao, "get_conn", _get_conn),
        patch.object(rewrite_service, "get_conn", _get_conn),
    ):
        yield conn
    conn.close()


def _long_content() -> str:
    """超过触发重写最小长度的正文（评分中性，不主动触发任何维度）。"""
    return ("陈墨沿着青石路走向镇口，天色暗下来，他把斗笠压得更低。" * 8)


def _scores_dict(total: float, **overrides) -> dict:
    dims = {f"D{i}": 3 for i in range(1, 11)}
    dims.update(overrides)
    return {"dimensions": dims, "total": total, "method": "heuristic"}


def _patch_llm_services(**kwargs):
    """集中 patch 重写链路依赖的 LLM 服务。"""
    return patch.multiple(
        rewrite_service,
        **kwargs,
    )


# ══════════════════════════════════════════════════════
# 1. chapter_rewrites DAO
# ══════════════════════════════════════════════════════

def test_insert_and_get_rewrite(rewrite_db):
    rid = chapter_rewrites_dao.insert_rewrite(
        "proj-1", "ch-1", REWRITE_TYPE_OPENING,
        original="原文",
        rewritten="重写稿",
        reason="开篇建立评分 2/5",
        score_before=5.0,
        score_after=7.0,
        status=chapter_rewrites_dao.REWRITE_STATUS_APPLIED,
    )
    assert rid
    record = chapter_rewrites_dao.get_rewrite(rid)
    assert record["project_id"] == "proj-1"
    assert record["chapter_id"] == "ch-1"
    assert record["rewrite_type"] == REWRITE_TYPE_OPENING
    assert record["original"] == "原文"
    assert record["rewritten"] == "重写稿"
    assert record["reason"] == "开篇建立评分 2/5"
    assert record["score_before"] == 5.0
    assert record["score_after"] == 7.0
    assert record["status"] == "applied"


def test_insert_rewrite_audit_is_append_only(rewrite_db):
    """审计不可变：同项目同章多次重写各生成独立记录，不做覆盖。"""
    first = chapter_rewrites_dao.insert_rewrite("proj-1", "ch-1", "opening", original="A", rewritten="A2")
    second = chapter_rewrites_dao.insert_rewrite("proj-1", "ch-1", "dialogue", original="B", rewritten="B2")
    assert first != second
    assert chapter_rewrites_dao.count_rewrites("proj-1") == 2
    assert chapter_rewrites_dao.count_rewrites("proj-1", chapter_id="ch-1") == 2


def test_invalid_status_normalized_to_proposed(rewrite_db):
    rid = chapter_rewrites_dao.insert_rewrite("proj-1", "ch-1", "opening", original="A", rewritten="B", status="bogus")
    assert chapter_rewrites_dao.get_rewrite(rid)["status"] == "proposed"


def test_update_status_and_filter_by_chapter(rewrite_db):
    rid = chapter_rewrites_dao.insert_rewrite("proj-1", "ch-1", "ending", original="A", rewritten="B")
    assert chapter_rewrites_dao.update_status(rid, chapter_rewrites_dao.REWRITE_STATUS_REVERTED) is True
    assert chapter_rewrites_dao.get_rewrite(rid)["status"] == "reverted"
    records = chapter_rewrites_dao.list_rewrites("proj-1", chapter_id="ch-1")
    assert len(records) == 1
    assert chapter_rewrites_dao.list_rewrites("proj-1", chapter_id="other") == []


# ══════════════════════════════════════════════════════
# 2. prompt_contract + ai_service 注入
# ══════════════════════════════════════════════════════

def test_priority_block_has_8_levels():
    block = build_priority_block()
    for level in ("1.事实约束", "2.剧情目标", "3.人物动机", "4.上章承接",
                   "5.节奏", "6.伏笔", "7.文风契约", "8.字数"):
        assert level in block


def test_rubric_generation_instructions_covers_d1_d10():
    block = build_rubric_generation_instructions()
    for dim in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"):
        assert dim in block
        assert RUBRIC_TO_GENERATION_INSTRUCTIONS[dim]


def test_full_block_combines_priority_and_rubric():
    full = build_full_style_contract_block()
    assert "写作优先级" in full
    assert "文风与质量硬性指令" in full


def test_continue_prompt_does_not_inject_legacy_quality_hard_rules():
    """Phase 9 keeps quality guidance in the bounded scene layer, not this generic prompt."""
    from app.models.schemas import ContinueWritingRequest
    from app.services.ai_service import _build_continue_prompt

    req = ContinueWritingRequest(projectId="p1", chapterId="c1", wordCount=1500)
    prompt = _build_continue_prompt(req)
    assert "写作优先级" not in prompt
    assert "文风与质量硬性指令" not in prompt
    assert "D10" not in prompt


# ══════════════════════════════════════════════════════
# 3. heuristic_rubric_scores
# ══════════════════════════════════════════════════════

def test_heuristic_rubric_scores_structure():
    scores = heuristic_rubric_scores(_long_content())
    assert set(scores["dimensions"].keys()) == {f"D{i}" for i in range(1, 11)}
    assert all(1 <= v <= 5 for v in scores["dimensions"].values())
    assert 0 <= scores["total"] <= 10
    assert scores["method"] == "heuristic"


def test_heuristic_d9_from_hook():
    assert heuristic_rubric_scores(_long_content(), has_hook=True)["dimensions"]["D9"] == 5
    assert heuristic_rubric_scores(_long_content(), has_hook=False)["dimensions"]["D9"] == 1


def test_heuristic_d10_from_ai_artifacts():
    """AI 味重（detect_ai_artifacts 低分/high 项）→ D10 ≤ 2（量表 6.2 触发去 AI 味重写）。"""
    low = heuristic_rubric_scores(
        _long_content(),
        ai={"score": 60, "debt_items": [{"severity": "high", "type": "句式重复"}]},
    )
    assert low["dimensions"]["D10"] <= 2
    high = heuristic_rubric_scores(
        _long_content(),
        ai={"score": 95, "debt_items": []},
    )
    assert high["dimensions"]["D10"] == 5


# ══════════════════════════════════════════════════════
# 4. 5 种重写类型分发（_do_rewrite）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_do_rewrite_opening_uses_review_opening():
    with patch.object(rewrite_service.review_service, "rewrite_opening", new=AsyncMock(return_value="新开头")) as m:
        result = await rewrite_service._do_rewrite("p1", "正文内容", REWRITE_TYPE_OPENING)
    assert result == "新开头"
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_rewrite_ending_uses_hook_rewrite():
    with patch.object(rewrite_service.hook_service, "rewrite_ending", new=AsyncMock(return_value="新结尾")) as m:
        result = await rewrite_service._do_rewrite("p1", "正文内容", REWRITE_TYPE_ENDING)
    assert result == "新结尾"
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_rewrite_emotion_uses_deai():
    with patch.object(rewrite_service.deai_service, "deai_rewrite", new=AsyncMock(return_value={"result": "去味正文"})) as m:
        result = await rewrite_service._do_rewrite("p1", "正文内容", REWRITE_TYPE_EMOTION)
    assert result == "去味正文"
    m.assert_awaited_once_with("p1", "正文内容")


@pytest.mark.asyncio
async def test_do_rewrite_dialogue_and_action_use_llm():
    for rtype in (REWRITE_TYPE_DIALOGUE, REWRITE_TYPE_ACTION):
        with patch.object(rewrite_service, "llm", spec=["chat"]) as mock_llm:
            # 重写结果必须 ≥ MIN_SECTION_REWRITTEN_LENGTH(40) 才不会降级
            mock_llm.chat = AsyncMock(
                return_value="重写后的段落内容，人物对话与动作描写更加具体生动，节奏紧凑，完全不会触发任何降级逻辑。"
            )
            result = await rewrite_service._do_rewrite("p1", _long_content(), rtype)
        assert "重写后的段落内容" in result
        assert mock_llm.chat.call_count == 1


@pytest.mark.asyncio
async def test_do_rewrite_unknown_type_returns_original():
    assert await rewrite_service._do_rewrite("p1", "正文内容", "bogus") == "正文内容"


# ══════════════════════════════════════════════════════
# 5. 自动回退守卫（_rewrite_with_guard）
# ══════════════════════════════════════════════════════

def _patch_guard(fact_result="PASS", before=5.0, after=7.0, hook_result=None):
    """patch 守卫依赖：事实核查 / 评分 / 结尾钩子判定（返回可用的上下文管理器）。"""
    def _scores(content, previous_text="", ai=None, has_hook=None):
        # 用正文是否含「改进稿」判断 before/after
        return _scores_dict(after if "改进稿" in content else before)

    @contextmanager
    def _guard():
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    rewrite_service.fact_check_service, "check_chapter",
                    new=AsyncMock(return_value={"result": fact_result, "issues": []}),
                )
            )
            stack.enter_context(
                patch.object(
                    rewrite_service, "heuristic_rubric_scores", side_effect=_scores
                )
            )
            if hook_result is not None:
                stack.enter_context(
                    patch.object(
                        rewrite_service.hook_service, "check_hook",
                        new=AsyncMock(return_value={"has_hook": hook_result}),
                    )
                )
            yield

    return _guard()


@pytest.mark.asyncio
async def test_guard_applied_records_audit(rewrite_db):
    """重写通过校验 → content 更新 + 审计 applied + 评分前后落表。"""
    with patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value="改进稿" + _long_content()),
    ), _patch_guard():
        content, record = await rewrite_service._rewrite_with_guard(
            "proj-1", "ch-1", _long_content(), REWRITE_TYPE_EMOTION, "D10 评分 1/5"
        )

    assert "改进稿" in content
    assert record is not None
    assert record["status"] == chapter_rewrites_dao.REWRITE_STATUS_APPLIED
    assert record["reverted"] is False
    assert record["score_before"] == 5.0
    assert record["score_after"] == 7.0
    db_record = chapter_rewrites_dao.get_rewrite(record["rewrite_id"])
    assert db_record["original"] == _long_content()
    assert "改进稿" in db_record["rewritten"]
    assert db_record["status"] == "applied"


@pytest.mark.asyncio
async def test_guard_reverts_on_fact_fail(rewrite_db):
    """重写后事实核查 FAIL → 自动回退原文 + 审计 reverted。"""
    with patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value="改进稿" + _long_content()),
    ), _patch_guard(fact_result="FAIL"):
        content, record = await rewrite_service._rewrite_with_guard(
            "proj-1", "ch-1", _long_content(), REWRITE_TYPE_EMOTION, "D10 评分 1/5",
            memory_context={},  # 事实核查需要 memory_context 非 None 才会执行
        )

    assert content == _long_content()  # 回退到原文
    assert record is not None
    assert record["status"] == chapter_rewrites_dao.REWRITE_STATUS_REVERTED
    assert record["reverted"] is True
    assert "自动回退" in record["reason"]


@pytest.mark.asyncio
async def test_guard_reverts_on_score_drop(rewrite_db):
    """重写后综合评分下降（5.0 → 3.0）→ 自动回退原文。"""
    with patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value="改进稿" + _long_content()),
    ), _patch_guard(before=5.0, after=3.0):
        content, record = await rewrite_service._rewrite_with_guard(
            "proj-1", "ch-1", _long_content(), REWRITE_TYPE_DIALOGUE, "D3 评分 2/5"
        )

    assert content == _long_content()
    assert record["status"] == chapter_rewrites_dao.REWRITE_STATUS_REVERTED
    assert "综合评分下降" in record["reason"]


@pytest.mark.asyncio
async def test_guard_reverts_ending_without_hook(rewrite_db):
    """结尾重写后仍无强钩 → 回退原文。"""
    with patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value="改进稿" + _long_content()),
    ), _patch_guard(hook_result=False):
        content, record = await rewrite_service._rewrite_with_guard(
            "proj-1", "ch-1", _long_content(), REWRITE_TYPE_ENDING, "D9 评分 1/5"
        )

    assert content == _long_content()
    assert record["status"] == chapter_rewrites_dao.REWRITE_STATUS_REVERTED
    assert "未形成强钩子" in record["reason"]


@pytest.mark.asyncio
async def test_guard_degrades_on_llm_failure(rewrite_db):
    """LLM 重写失败（返回原文）→ 不落审计、正文不变、不抛异常。"""
    with patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value=_long_content()),  # 无变化
    ):
        content, record = await rewrite_service._rewrite_with_guard(
            "proj-1", "ch-1", _long_content(), REWRITE_TYPE_ACTION, "D5 评分 2/5"
        )

    assert content == _long_content()
    assert record is None
    assert chapter_rewrites_dao.count_rewrites("proj-1") == 0


@pytest.mark.asyncio
async def test_guard_skips_short_content():
    """正文过短（< 200 字）→ 直接跳过，不调用 LLM。"""
    with patch.object(rewrite_service, "_do_rewrite", new=AsyncMock()) as m:
        content, record = await rewrite_service._rewrite_with_guard(
            "proj-1", "ch-1", "太短了。", REWRITE_TYPE_EMOTION, "原因"
        )
    assert content == "太短了。"
    assert record is None
    m.assert_not_awaited()


# ══════════════════════════════════════════════════════
# 6. run_style_rewrite_chain 编排
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chain_high_scores_no_op(rewrite_db):
    """全维度高分 → 无重写、无债务、正文不变。"""
    with patch.object(
        rewrite_service, "heuristic_rubric_scores",
        return_value=_scores_dict(8.5, D1=4, D3=4, D5=4, D6=4, D9=5, D10=5),
    ):
        result = await rewrite_service.run_style_rewrite_chain(
            "proj-1", "ch-1", _long_content()
        )
    assert result["rewrite_count"] == 0
    assert result["debt_count"] == 0
    assert result["content"] == _long_content()


@pytest.mark.asyncio
async def test_chain_d10_triggers_emotion_rewrite(rewrite_db):
    """D10 ≤2（AI 痕迹）→ 触发情绪/去 AI 味重写，审计 applied，统计正确。"""
    with patch.object(
        rewrite_service, "heuristic_rubric_scores",
        return_value=_scores_dict(5.0, D10=1),
    ), patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value="改进稿" + _long_content()),
    ):
        result = await rewrite_service.run_style_rewrite_chain(
            "proj-1", "ch-1", _long_content()
        )

    assert result["rewrite_count"] == 1
    assert "改进稿" in result["content"]
    assert len(result["rewrites"]) == 1
    assert result["rewrites"][0]["dimension"] == "D10"
    assert result["rewrites"][0]["status"] == "applied"
    # 审计落表
    assert chapter_rewrites_dao.count_rewrites("proj-1") == 1


@pytest.mark.asyncio
async def test_chain_manual_dimension_debt_only(rewrite_db):
    """「必须人工」维度（D2/D4/D7/D8）低分 → 只记质量债，不自动重写。"""
    with patch.object(
        rewrite_service, "heuristic_rubric_scores",
        return_value=_scores_dict(4.0, D2=1, D4=2, D7=1, D8=2),
    ), patch.object(rewrite_service, "_do_rewrite", new=AsyncMock()) as m:
        result = await rewrite_service.run_style_rewrite_chain(
            "proj-1", "ch-1", _long_content()
        )

    assert result["rewrite_count"] == 0
    assert result["debt_count"] == 4
    assert {item["dimension"] for item in result["debt_items"]} == {"D2", "D4", "D7", "D8"}
    assert all(item["manual"] for item in result["debt_items"])
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_first_chapter_opening_debt_only(rewrite_db):
    """第一章开篇（D1）≤2 → 量表 6.2 需整章重写（人工），只记债务不自动局部重写。"""
    with patch.object(
        rewrite_service, "heuristic_rubric_scores",
        return_value=_scores_dict(4.0, D1=1),
    ), patch.object(rewrite_service, "_do_rewrite", new=AsyncMock()) as m:
        result = await rewrite_service.run_style_rewrite_chain(
            "proj-1", "ch-1", _long_content(), is_first_chapter=True
        )

    assert result["rewrite_count"] == 0
    assert result["debt_count"] == 1
    assert "整章重写" in result["debt_items"][0]["detail"]
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_second_chapter_opening_rewrites(rewrite_db):
    """非第一章开篇（D1）≤2 → 触发开头局部重写。"""
    with patch.object(
        rewrite_service, "heuristic_rubric_scores",
        return_value=_scores_dict(4.0, D1=1),
    ), patch.object(
        rewrite_service, "_do_rewrite",
        new=AsyncMock(return_value="改进稿" + _long_content()),
    ):
        result = await rewrite_service.run_style_rewrite_chain(
            "proj-1", "ch-1", _long_content(), is_first_chapter=False
        )

    assert result["rewrite_count"] == 1
    assert result["rewrites"][0]["rewrite_type"] == REWRITE_TYPE_OPENING


# ══════════════════════════════════════════════════════
# 7. apply_rewrite（应用重写 / 人工确认入口）
# ══════════════════════════════════════════════════════

def test_apply_rewrite_updates_chapter(rewrite_db):
    """应用重写：chapters.content ← rewritten，状态 → applied。"""
    rewritten_text = "应用后的完整正文。"
    rid = chapter_rewrites_dao.insert_rewrite(
        "proj-1", "ch-1", "emotion",
        original="原文", rewritten=rewritten_text, reason="D10 评分 1/5",
    )
    conn = rewrite_db
    conn.execute(
        "INSERT INTO chapters (id, project_id, title, content, word_count, status, created_at, updated_at) "
        "VALUES ('ch-1', 'proj-1', '第一章', '原文', 2, 'completed', '2026-08-13', '2026-08-13')"
    )
    conn.commit()

    record = rewrite_service.apply_rewrite(rid)
    assert record is not None
    assert record["status"] == "applied"
    row = conn.execute("SELECT content, word_count FROM chapters WHERE id='ch-1'").fetchone()
    assert row["content"] == rewritten_text
    assert row["word_count"] == len(rewritten_text)


def test_apply_rewrite_rejected_for_reverted(rewrite_db):
    """已被自动回退（reverted）的记录不允许应用 → ValueError。"""
    rid = chapter_rewrites_dao.insert_rewrite(
        "proj-1", "ch-1", "opening",
        original="原文", rewritten="重写稿", status=chapter_rewrites_dao.REWRITE_STATUS_REVERTED,
    )
    with pytest.raises(ValueError):
        rewrite_service.apply_rewrite(rid)


def test_apply_rewrite_not_found(rewrite_db):
    assert rewrite_service.apply_rewrite("nonexistent") is None


# ══════════════════════════════════════════════════════
# 8. API 端点
# ══════════════════════════════════════════════════════

def _unique_name(prefix: str = "rw") -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def _create_project(client: TestClient) -> dict:
    resp = client.post("/api/projects/", json={"name": _unique_name(), "genre": "xuanhuan"})
    assert resp.status_code == 200, f"创建项目失败: {resp.text}"
    return resp.json()["data"]


def _delete_project(client: TestClient, project_id: str) -> None:
    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code in (200, 404, 409), f"删除失败: {resp.status_code}"


def test_rewrites_api_list_empty():
    with TestClient(app) as client:
        project = _create_project(client)
        try:
            resp = client.get(f"/api/rewrites?projectId={project['id']}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["data"]["count"] == 0
        finally:
            _delete_project(client, project["id"])


def test_rewrites_api_run_with_patched_service():
    payload = {
        "content": "改进后的正文",
        "rewrite_count": 1,
        "debt_count": 1,
        "debt_items": [],
        "rewrites": [{"rewrite_id": "r1", "rewrite_type": "emotion", "status": "applied"}],
    }
    with patch(
        "app.api.rewrites.rewrite_service.run_style_rewrite_chain",
        new=AsyncMock(return_value=payload),
    ):
        with TestClient(app) as client:
            project = _create_project(client)
            try:
                resp = client.post(
                    "/api/rewrites/run",
                    json={"projectId": project["id"], "chapterId": "ch-1", "text": "正文"},
                )
                assert resp.status_code == 200
                data = resp.json()["data"]
                assert data["rewrite_count"] == 1
                assert data["rewrites"][0]["rewrite_type"] == "emotion"
            finally:
                _delete_project(client, project["id"])


def test_rewrites_api_apply_not_found():
    with TestClient(app) as client:
        resp = client.post("/api/rewrites/nonexistent/apply")
        assert resp.status_code == 404


def test_rewrites_api_project_not_found():
    with TestClient(app) as client:
        resp = client.get("/api/rewrites?projectId=nonexistent-id")
        assert resp.status_code == 404
