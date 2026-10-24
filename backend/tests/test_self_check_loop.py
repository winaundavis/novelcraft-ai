"""M3 自检闭环编排层单测 —— novel_generation_service._run_self_check_loop。

QA P2-1：编排层直接单测（此前只有各服务层单测，P1「超阈值局部重写」
正是因缺少编排层覆盖而漏测）。覆盖：

- 正常链路：事实核查 → 钩子检测 → AI 味检测，统计累计正确；
- 各环节降级：任一服务抛异常 → 不抛出、统计不崩、返回原 content；
- P1 行为：detect_ai_artifacts 返回 needs_rewrite=True → 触发开头局部重写且
  rewrite_count+1；False 时不重写；
- 钩子循环重写（P2-3）：无钩时按 MAX_HOOK_REWRITES 上限循环重写，降级即停止；
- P2-2：_run_generation_job 重试成功分支同样执行自检闭环（与主路径一致）。

所有服务调用均 AsyncMock / patch 打桩（秒级完成，不等待真实 LLM）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.dao import chapter_hooks as chapter_hooks_dao
from app.dao import novel_jobs as novel_jobs_dao
from app.services import fact_check_service
from app.services import hook_service
from app.services import memory_service
from app.services import review_service
from app.services import novel_generation_service as ngs


# ── Fixtures / Helpers ────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_runtime():
    """清空模块级运行时状态，避免用例间相互影响。"""
    ngs._jobs.clear()
    ngs._job_subscribers.clear()
    yield
    ngs._jobs.clear()
    ngs._job_subscribers.clear()


@pytest.fixture
def job():
    return ngs.NovelGenerationJob(
        id="job-1",
        project_id="proj-1",
        mode="all",
        requested_chapter_id=None,
        status="running",
        message="",
        target_words=0,
        total_chapters=1,
    )


@pytest.fixture
def chapter():
    return {"id": "c1", "title": "第一章", "order": 1, "parentId": "v1"}


@pytest.fixture
def self_check_mocks():
    """patch 自检闭环三个服务 + 上一章文本读取，暴露 mock 句柄。

    detect_ai_artifacts 为同步函数（确定性规则），用 return_value 打桩；
    rewrite_opening 为 async（LLM），用 AsyncMock 打桩。
    """
    with (
        patch.object(fact_check_service, "run_fact_check_loop", new=AsyncMock()) as m_fc,
        patch.object(hook_service, "update_previous_hooks", new=AsyncMock()) as m_upd,
        patch.object(hook_service, "check_hook", new=AsyncMock()) as m_check,
        patch.object(hook_service, "rewrite_ending", new=AsyncMock()) as m_rew,
        patch.object(hook_service, "record_hook", new=Mock()) as m_rec,
        patch.object(
            review_service, "detect_ai_artifacts",
            return_value={"issues": [], "debt_items": [], "needs_rewrite": False, "score": 100},
        ) as m_detect,
        patch.object(review_service, "rewrite_opening", new=AsyncMock()) as m_ropen,
        patch.object(ngs, "_load_existing_chapter_text", return_value="") as m_prev,
    ):
        # 默认：事实核查直接通过、钩子检测有钩、AI 味零命中
        m_fc.return_value = {"content": None, "stats": {}}
        m_check.return_value = {"has_hook": True, "hook_type": "suspense", "description": "悬念"}
        m_rew.return_value = ""
        m_ropen.return_value = ""
        yield {
            "fact_check": m_fc,
            "update_previous_hooks": m_upd,
            "check_hook": m_check,
            "rewrite_ending": m_rew,
            "record_hook": m_rec,
            "detect_ai": m_detect,
            "rewrite_opening": m_ropen,
            "load_prev": m_prev,
        }


async def _run(job, chapter, content, **kwargs):
    """调用 _run_self_check_loop 的便捷封装。"""
    return await ngs._run_self_check_loop(
        job=job,
        chapter=chapter,
        result={"content": content},
        memory_context=None,
        previous_id=kwargs.get("previous_id"),
        next_id=kwargs.get("next_id", "c2"),
        hook_instruction=kwargs.get("hook_instruction", ""),
        apply_deterministic_gate=False,
    )


# ══════════════════════════════════════════════════════
# 1. 正常链路：统计累计正确
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_normal_chain_accumulates_stats(job, chapter, self_check_mocks):
    """正常链路：事实核查 1 次重写 1 次 + 钩子有钩 + AI 味 1 条质量债 → 统计正确。"""
    m = self_check_mocks
    content = "第一章正文内容。"
    m["fact_check"].return_value = {
        "content": content,
        "stats": {"fact_check_count": 1, "rewrite_count": 1},
    }
    m["detect_ai"].return_value = {
        "issues": [],
        "debt_items": [{"type": "sentence_repeat", "severity": "medium", "detail": "句式重复率 40%"}],
        "needs_rewrite": False,
        "score": 90,
    }

    result = await _run(job, chapter, content)

    assert result["content"] == content
    assert result["fact_check_count"] == 1
    assert result["rewrite_count"] == 1
    assert result["hook_miss_count"] == 0
    assert result["quality_debt_count"] == 1
    m["record_hook"].assert_called_once()
    m["rewrite_opening"].assert_not_awaited()


@pytest.mark.asyncio
async def test_fact_check_rewrite_updates_content_and_stats(job, chapter, self_check_mocks):
    """事实核查返回重写正文 + 统计 → content 更新、统计累计。"""
    m = self_check_mocks
    original = "第一章原文"
    fc_content = "事实核查修正后的正文"
    m["fact_check"].return_value = {
        "content": fc_content,
        "stats": {"fact_check_count": 2, "rewrite_count": 1},
    }

    result = await _run(job, chapter, original)

    assert result["content"] == fc_content
    assert result["fact_check_count"] == 2
    assert result["rewrite_count"] == 1


@pytest.mark.asyncio
async def test_previous_chapter_context_loaded(job, chapter, self_check_mocks):
    """有上一章时：读取上章文本、更新其钩子状态、detect_ai 收到前文。"""
    m = self_check_mocks
    content = "第一章正文"
    m["load_prev"].return_value = "上一章正文"

    await _run(job, chapter, content, previous_id="c_prev")

    args = m["update_previous_hooks"].await_args.args
    assert args[:3] == ("proj-1", "c_prev", content)
    assert callable(args[3])
    m["load_prev"].assert_called_once_with("proj-1", "c_prev")
    m["detect_ai"].assert_called_once_with(content, "上一章正文")


# ══════════════════════════════════════════════════════
# 2. 钩子检测：未留强钩 → 结尾重写（上限 MAX_HOOK_REWRITES 次）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_hook_miss_rewrites_ending_once(job, chapter, self_check_mocks):
    """无钩 → 重写结尾 + 复检通过：rewrite_count=1、hook_miss_count=1。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    new_ending = "重写后的结尾：他推开门，走廊尽头传来一声轻响。"
    m["check_hook"].side_effect = [
        {"has_hook": False, "hook_type": None, "description": "平铺收尾"},
        {"has_hook": True, "hook_type": "suspense", "description": "悬念"},
    ]
    m["rewrite_ending"].return_value = new_ending

    result = await _run(job, chapter, original)

    assert result["hook_miss_count"] == 1
    assert result["rewrite_count"] == 1
    assert result["content"] == new_ending
    assert m["check_hook"].call_count == 2


@pytest.mark.asyncio
async def test_hook_loop_rewrites_up_to_max(job, chapter, self_check_mocks):
    """重写后仍无钩 → 循环重写至 MAX_SELF_CHECK_HOOK_REWRITES 上限，hook_miss 只计一次。

    Codex P1-2：单章钩子重写预算从 MAX_HOOK_REWRITES(2) 压到 MAX_SELF_CHECK_HOOK_REWRITES(1)，
    只做一次定向重写，其余记 hook_miss。
    """
    m = self_check_mocks
    original = "第一章正文内容。"
    m["check_hook"].return_value = {"has_hook": False, "hook_type": None, "description": "始终无钩"}
    m["rewrite_ending"].side_effect = ["结尾版本一", "结尾版本二", "结尾版本三"]

    result = await _run(job, chapter, original)

    assert result["hook_miss_count"] == 1  # 只计一次
    assert result["rewrite_count"] == ngs.MAX_SELF_CHECK_HOOK_REWRITES  # 1
    assert result["content"] == "结尾版本一"  # 第 1 次重写后停止
    assert m["rewrite_ending"].call_count == ngs.MAX_SELF_CHECK_HOOK_REWRITES
    assert m["check_hook"].call_count == ngs.MAX_SELF_CHECK_HOOK_REWRITES + 1


@pytest.mark.asyncio
async def test_hook_rewrite_degrade_stops_loop(job, chapter, self_check_mocks):
    """重写降级（返回原文/无变化）→ 循环立即停止，不计 rewrite。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["check_hook"].return_value = {"has_hook": False, "hook_type": None, "description": "无钩"}
    m["rewrite_ending"].return_value = original  # 降级返回原文

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["rewrite_count"] == 0
    assert result["hook_miss_count"] == 1
    assert m["rewrite_ending"].call_count == 1


# ══════════════════════════════════════════════════════
# 3. P1：AI 味超阈值 → 开头局部重写
# ══════════════════════════════════════════════════════

def _ai_result_needs_rewrite():
    return {
        "issues": [
            {
                "severity": "high", "type": "开头重复",
                "reason": "开头 30 字与上章相似度 80%", "suggestion": "改写开头",
            }
        ],
        "debt_items": [{"type": "opening_repeat", "severity": "high", "detail": "相似度 80%"}],
        "needs_rewrite": True,
        "score": 80,
    }


@pytest.mark.asyncio
async def test_p1_needs_rewrite_triggers_opening_rewrite(job, chapter, self_check_mocks):
    """P1 核心：needs_rewrite=True → 触发 rewrite_opening，rewrite_count+1，content 更新。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    rewritten = "重写后的开头。第一章正文继续……"
    ai_result = _ai_result_needs_rewrite()
    m["detect_ai"].return_value = ai_result
    m["rewrite_opening"].return_value = rewritten

    result = await _run(job, chapter, original)

    assert result["content"] == rewritten
    assert result["rewrite_count"] == 1
    assert result["quality_debt_count"] == 1
    m["rewrite_opening"].assert_awaited_once_with(original, ai_result)


@pytest.mark.asyncio
async def test_p1_no_needs_rewrite_skips_opening_rewrite(job, chapter, self_check_mocks):
    """P1：needs_rewrite=False → 只累计质量债，不触发开头重写。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["detect_ai"].return_value = {
        "issues": [],
        "debt_items": [{"type": "connective_overuse", "severity": "medium", "detail": "连接词 6.5/500字"}],
        "needs_rewrite": False,
        "score": 90,
    }

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["rewrite_count"] == 0
    assert result["quality_debt_count"] == 1
    m["rewrite_opening"].assert_not_awaited()


@pytest.mark.asyncio
async def test_p1_opening_rewrite_degrade_keeps_content(job, chapter, self_check_mocks):
    """P1 降级：needs_rewrite=True 但 rewrite_opening 返回原文 → 不重写、不计数、不阻塞。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["detect_ai"].return_value = _ai_result_needs_rewrite()
    m["rewrite_opening"].return_value = original  # LLM 失败降级返回原文

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["rewrite_count"] == 0
    assert result["quality_debt_count"] == 1


# ══════════════════════════════════════════════════════
# Codex P1-3：跨章整段重复 → 阻止章节通过（触发重写）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cross_chapter_repeat_high_triggers_rewrite(job, chapter, self_check_mocks):
    """跨章 ≥40 字整段重复 → 强制 needs_rewrite，触发重写，质量债累计。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    # detect_ai 返回无 high 项（needs_rewrite=False），但跨章检测注入 high 重复
    m["detect_ai"].return_value = {
        "issues": [],
        "debt_items": [],
        "needs_rewrite": False,
        "score": 100,
    }
    long_phrase = (
        "他缓缓抬起手，指尖凝聚起一缕幽蓝色的火焰，映照着他冷峻的面容，"
        "周围的空气仿佛都凝固了。"
    )
    m["load_prev"].return_value = ""  # 上一章文本（detect_ai 用）
    with patch.object(
        ngs, "_load_prior_chapter_texts", return_value=[f"前章内容。{long_phrase}后续。"]
    ), patch.object(
        review_service, "_cross_chapter_ngram_repeat",
        return_value=[{"phrase": long_phrase, "count": 1, "prior_chapter_index": 0, "length": len(long_phrase)}],
    ), patch.object(
        review_service, "rewrite_ngram_repeat", new=AsyncMock(return_value="重写后的正文。")
    ) as m_ngram_rewrite:
        result = await _run(job, chapter, original)

    assert result["content"] == "重写后的正文。"
    assert result["rewrite_count"] == 1
    assert result["quality_debt_count"] >= 1
    m_ngram_rewrite.assert_awaited_once()


@pytest.mark.asyncio
async def test_cross_chapter_repeat_medium_only_debt(job, chapter, self_check_mocks):
    """跨章 20-39 字重复（medium）→ 只记质量债，不强制重写。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["detect_ai"].return_value = {
        "issues": [],
        "debt_items": [],
        "needs_rewrite": False,
        "score": 100,
    }
    medium_phrase = "他停下脚步，抬头望向那轮残月，眼中闪过一丝决然。"
    with patch.object(
        ngs, "_load_prior_chapter_texts", return_value=[f"前章。{medium_phrase}后续。"]
    ), patch.object(
        review_service, "_cross_chapter_ngram_repeat",
        return_value=[{"phrase": medium_phrase, "count": 1, "prior_chapter_index": 0, "length": len(medium_phrase)}],
    ), patch.object(
        review_service, "rewrite_ngram_repeat", new=AsyncMock(return_value="")
    ) as m_ngram_rewrite:
        result = await _run(job, chapter, original)

    assert result["content"] == original  # 不重写
    assert result["rewrite_count"] == 0
    assert result["quality_debt_count"] == 1  # 只记质量债
    m_ngram_rewrite.assert_not_awaited()


# ══════════════════════════════════════════════════════
# 4. 各环节降级：不抛出、统计不崩、返回原 content
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fact_check_degradation(job, chapter, self_check_mocks):
    """事实核查抛异常 → 跳过该环节，其余环节正常执行。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["fact_check"].side_effect = RuntimeError("LLM 不可用")

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["fact_check_count"] == 0
    assert result["rewrite_count"] == 0
    m["record_hook"].assert_called_once()  # 后续钩子环节仍执行


@pytest.mark.asyncio
async def test_hook_degradation(job, chapter, self_check_mocks):
    """钩子检测抛异常 → 跳过该环节，不落钩、不计数。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["check_hook"].side_effect = RuntimeError("LLM 不可用")

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["hook_miss_count"] == 0
    m["record_hook"].assert_not_called()


@pytest.mark.asyncio
async def test_review_degradation(job, chapter, self_check_mocks):
    """AI 味检测抛异常 → 跳过该环节，质量债/重写均为 0。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["detect_ai"].side_effect = RuntimeError("检测异常")

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["quality_debt_count"] == 0
    assert result["rewrite_count"] == 0


# ══════════════════════════════════════════════════════
# 5. P2-2：_run_generation_job 重试成功分支执行自检闭环
# ══════════════════════════════════════════════════════

@pytest.fixture
def job_db():
    """仅 novel_jobs 表的内存库，驱动 _run_generation_job（其余依赖全部 mock）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS novel_jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            requested_chapter_id TEXT,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            target_words INTEGER NOT NULL DEFAULT 0,
            total_chapters INTEGER NOT NULL DEFAULT 0,
            completed_chapters INTEGER NOT NULL DEFAULT 0,
            failed_chapters INTEGER NOT NULL DEFAULT 0,
            fallback_chapters INTEGER NOT NULL DEFAULT 0,
            generated_words INTEGER NOT NULL DEFAULT 0,
            current_chapter_id TEXT,
            current_chapter_title TEXT NOT NULL DEFAULT '',
            last_completed_chapter_id TEXT,
            last_completed_chapter_title TEXT NOT NULL DEFAULT '',
            llm_provider TEXT NOT NULL DEFAULT '',
            llm_model TEXT NOT NULL DEFAULT '',
            last_generation_source TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 1,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            checkpoint_type TEXT NOT NULL DEFAULT '',
            resume_target_json TEXT NOT NULL DEFAULT '',
            pending_manual_recovery INTEGER NOT NULL DEFAULT 0,
            heartbeat_at TEXT NOT NULL DEFAULT '',
            fact_check_count INTEGER NOT NULL DEFAULT 0,
            rewrite_count INTEGER NOT NULL DEFAULT 0,
            hook_miss_count INTEGER NOT NULL DEFAULT 0,
            quality_debt_count INTEGER NOT NULL DEFAULT 0,
            escalation_count INTEGER NOT NULL DEFAULT 0
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
        """
    )
    conn.commit()

    def _get_conn():
        return conn

    with patch.object(ngs, "get_conn", _get_conn), \
         patch.object(novel_jobs_dao, "get_conn", _get_conn):
        yield conn
    conn.close()


@contextmanager
def _patch_generation_job_deps(*, generate_side_effect, self_check_return):
    """mock _run_generation_job 的全部外部依赖，返回 (chapter, mocks)。

    注意：patch.multiple 返回的 dict 不含下划线开头的键，故改用 patch.object
    逐个打桩并收集句柄。
    """
    chapter = {"id": "c1", "title": "第一章", "order": 1, "parentId": "v1"}
    project = {
        "outline": {"mainSummary": "测试大纲", "nodes": [chapter]},
        "premise": "测试",
        "subtitle": "",
        "characters": [],
        "worldRules": [],
        "styleConfig": {},
    }
    with (
        patch.object(ngs, "_load_project", return_value=project) as m_load_project,
        patch.object(ngs, "_chapter_nodes_from_outline", return_value=[chapter]) as m_nodes,
        patch.object(ngs, "_select_chapter_nodes", return_value=[chapter]) as m_select,
        patch.object(ngs, "_load_previous_summary", return_value="") as m_prev_sum,
        patch.object(ngs, "_build_memory_context", return_value=None) as m_mem_ctx,
        patch.object(ngs, "_chapter_id_before", return_value=None) as m_before,
        patch.object(ngs, "_chapter_id_after", return_value=None) as m_after,
        patch.object(ngs, "_load_project_word_count", return_value=0) as m_word,
        patch.object(
            ngs, "_generate_chapter_with_expansion",
            new=AsyncMock(side_effect=generate_side_effect),
        ) as m_gen,
        patch.object(
            ngs, "_run_self_check_loop",
            new=AsyncMock(return_value=self_check_return),
        ) as m_self_check,
        patch.object(
            ngs, "_persist_chapter_and_sync_memory",
            new=AsyncMock(return_value="本章摘要"),
        ) as m_persist,
        patch.object(memory_service, "ensure_volumes", new=Mock()),
        patch.object(chapter_hooks_dao, "list_open_hooks", return_value=[]),
    ):
        mocks = {
            "_load_project": m_load_project,
            "_chapter_nodes_from_outline": m_nodes,
            "_select_chapter_nodes": m_select,
            "_load_previous_summary": m_prev_sum,
            "_build_memory_context": m_mem_ctx,
            "_chapter_id_before": m_before,
            "_chapter_id_after": m_after,
            "_load_project_word_count": m_word,
            "_generate_chapter_with_expansion": m_gen,
            "_run_self_check_loop": m_self_check,
            "_persist_chapter_and_sync_memory": m_persist,
        }
        yield chapter, mocks


def _new_job(ngs_module, job_id="job-retry"):
    job = ngs_module.NovelGenerationJob(
        id=job_id,
        project_id="proj-1",
        mode="all",
        requested_chapter_id=None,
        status="queued",
        message="",
        target_words=0,
        total_chapters=1,
        max_retries=1,
        started_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
    )
    ngs_module._jobs[job.id] = job
    return job


@pytest.mark.asyncio
async def test_retry_success_runs_self_check_loop(job_db):
    """QA P2-2：首次生成失败 → 重试成功分支同样执行自检闭环并合并统计。"""
    attempts = {"n": 0}

    async def generate_with_retry(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("首次生成失败")
        return {
            "content": "第一章正文内容。",
            "used_fallback": False,
            "last_source": "llm",
            "provider": "mock",
            "model": "mock-model",
        }

    self_check_return = {
        "content": "自检后的正文。",
        "fact_check_count": 1,
        "rewrite_count": 1,
        "hook_miss_count": 1,
        "quality_debt_count": 1,
    }
    job = _new_job(ngs)

    with _patch_generation_job_deps(
        generate_side_effect=generate_with_retry,
        self_check_return=self_check_return,
    ) as (chapter, mocks):
        await ngs._run_generation_job(job.id)

    assert job.status == "completed"
    assert job.completed_chapters == 1
    assert job.retry_count == 1
    assert job.failed_chapters == 0
    # 重试成功分支执行了自检闭环（调用 1 次：重试成功后），统计正确合并
    assert mocks["_run_self_check_loop"].call_count == 1
    assert job.fact_check_count == 1
    assert job.rewrite_count == 1
    assert job.hook_miss_count == 1
    assert job.quality_debt_count == 1
    # 重试成功后的自检结果进入落库
    persist_call = mocks["_persist_chapter_and_sync_memory"].call_args
    assert persist_call.kwargs["result"]["content"] == "自检后的正文。"


@pytest.mark.asyncio
async def test_main_success_runs_self_check_loop(job_db):
    """对照：主路径（首次即成功）同样执行自检闭环（与重试路径一致）。"""
    async def generate_ok(**kwargs):
        return {
            "content": "第一章正文内容。",
            "used_fallback": False,
            "last_source": "llm",
            "provider": "mock",
            "model": "mock-model",
        }

    self_check_return = {
        "content": "自检后的正文。",
        "fact_check_count": 2,
        "rewrite_count": 0,
        "hook_miss_count": 0,
        "quality_debt_count": 0,
    }
    job = _new_job(ngs, job_id="job-main")

    with _patch_generation_job_deps(
        generate_side_effect=generate_ok,
        self_check_return=self_check_return,
    ) as (chapter, mocks):
        await ngs._run_generation_job(job.id)

    assert job.status == "completed"
    assert mocks["_run_self_check_loop"].call_count == 1
    assert job.fact_check_count == 2
    assert job.rewrite_count == 0
    assert job.hook_miss_count == 0
    assert job.quality_debt_count == 0


# ══════════════════════════════════════════════════════
# Codex P2 复测 P1：事实冲突质量门槛
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fact_conflict_pending_triggers_rewrite(job, chapter, self_check_mocks):
    """事实核查未通过（不可调和冲突）→ 计入质量债 + 强制触发重写（阻止静默通过）。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    # 事实核查返回 passed=False + conflict
    m["fact_check"].return_value = {
        "content": original,
        "passed": False,
        "stats": {"fact_check_count": 2, "rewrite_count": 1},
        "conflict": {"summary": "沈青山死亡时间线矛盾", "root_cause": "章节称七年后回来，账本称死于大火"},
    }
    # detect_ai 返回无 high 项（needs_rewrite=False），但事实冲突应强制重写
    m["detect_ai"].return_value = {
        "issues": [],
        "debt_items": [],
        "needs_rewrite": False,
        "score": 100,
    }
    rewritten = "修正冲突后的正文。"
    m["rewrite_opening"].return_value = rewritten

    result = await _run(job, chapter, original)

    # 事实冲突计入质量债
    assert result["quality_debt_count"] >= 1
    # 强制触发重写（rewrite_opening 被调用）
    m["rewrite_opening"].assert_awaited()
    assert result["content"] == rewritten
    assert result["rewrite_count"] >= 1


@pytest.mark.asyncio
async def test_fact_conflict_pending_rewrite_degrade_keeps_content(job, chapter, self_check_mocks):
    """事实冲突重写降级（返回原文）→ 不崩溃、保留原文、质量债仍计入。"""
    m = self_check_mocks
    original = "第一章正文内容。"
    m["fact_check"].return_value = {
        "content": original,
        "passed": False,
        "stats": {"fact_check_count": 1, "rewrite_count": 0},
        "conflict": {"summary": "角色身份矛盾", "root_cause": "陈秋渡身份重叠"},
    }
    m["detect_ai"].return_value = {
        "issues": [],
        "debt_items": [],
        "needs_rewrite": False,
        "score": 100,
    }
    m["rewrite_opening"].return_value = original  # 降级返回原文

    result = await _run(job, chapter, original)

    assert result["content"] == original
    assert result["quality_debt_count"] >= 1  # 冲突质量债仍计入
    assert result["rewrite_count"] == 0  # 重写无变化不计数
