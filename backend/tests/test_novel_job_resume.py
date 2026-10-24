"""M2 断点续传单元/集成测试（T2）

覆盖验收点：
- 重启后进行中任务进入「可恢复」状态（interrupted + recoverable）
- resume_job 基于 DB 章节内容自动跳过已生成章节，不重跑（mock 计数验证）
- 恢复时前文记忆（上一章结尾 / 章摘要）从 DB 读取
- 失败章写入 checkpoint（pending_manual_recovery），恢复时跳过该章继续
- 不可恢复场景（已完成 / 全部生成完 / 项目已有运行中任务）正确拒绝
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import chapter_commits as cc_dao
from app.dao import chapters as chapters_dao
from app.dao import chapter_hooks as chapter_hooks_dao
from app.dao import novel_facts as novel_facts_dao
from app.dao import novel_jobs as novel_jobs_dao
from app.dao import projects as projects_dao
from app.dao import volumes as volumes_dao
from app.services import fact_check_service
from app.services import memory_service
from app.services import novel_generation_service as ngs


# ══════════════════════════════════════════════════════
# Fixtures / Helpers
# ══════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_runtime():
    """清空模块级运行时状态，避免用例间相互影响；并屏蔽 M3 自检 LLM（秒级降级）。"""
    ngs._jobs.clear()
    ngs._job_subscribers.clear()

    async def _fake_llm(messages, **kwargs):
        system = messages[0]["content"]
        if "节奏编辑" in system:  # hook check → 有钩，不触发结尾重写
            return '{"has_hook": true, "hook_type": "suspense", "description": "章末留下悬念"}'
        if "连续性编辑" in system:  # 上一章钩子承接 → carried
            return '{"resolved": false}'
        return '{"result": "PASS", "issues": []}'  # 事实核查 → 一次通过

    # fact_check_service.llm 与 hook_service.llm 是同一实例，patch 一次即可
    with patch.object(fact_check_service.llm, "chat", new=AsyncMock(side_effect=_fake_llm)), \
         patch.object(ngs.llm, "is_configured", return_value=True):
        yield
    ngs._jobs.clear()
    ngs._job_subscribers.clear()


@pytest.fixture
def resume_db():
    """多表内存库（projects/chapters/volumes/novel_facts/novel_jobs），
    patch 相关 DAO 与 ngs 的 get_conn。"""
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
        CREATE TABLE IF NOT EXISTS chapter_commits (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            context_hash TEXT NOT NULL DEFAULT '',
            audit_id TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(project_id, chapter_id)
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
         patch.object(novel_jobs_dao, "get_conn", _get_conn), \
         patch.object(projects_dao, "get_conn", _get_conn), \
         patch.object(chapters_dao, "get_conn", _get_conn), \
         patch.object(volumes_dao, "get_conn", _get_conn), \
         patch.object(novel_facts_dao, "get_conn", _get_conn), \
         patch.object(chapter_hooks_dao, "get_conn", _get_conn), \
         patch.object(cc_dao, "get_conn", _get_conn):
        yield conn

    conn.close()


@pytest.fixture
def memory_mocks():
    """屏蔽记忆同步中的 LLM 调用（全部降级，不阻塞主流程）。"""
    with patch.object(memory_service, "summarize_chapter", new=AsyncMock(return_value="本章摘要")), \
         patch.object(memory_service, "extract_facts", new=AsyncMock(return_value=[])), \
         patch.object(memory_service, "record_chapter_completed", new=AsyncMock()), \
         patch.object(memory_service, "compress_volume_summary", new=AsyncMock(return_value="")):
        yield


def _make_outline(chapter_count: int = 5) -> dict:
    nodes = [
        {"id": "v1", "type": "volume", "title": "卷一", "order": 1},
    ]
    for i in range(1, chapter_count + 1):
        nodes.append(
            {
                "id": f"c{i}",
                "type": "chapter",
                "title": f"第{i}章",
                "order": i,
                "parentId": "v1",
                "summary": f"第{i}章摘要",
                "targetWordCount": 2000,
            }
        )
    return {"mainSummary": "测试大纲", "nodes": nodes}


def _insert_project(project_id: str = "proj-1", chapter_count: int = 5) -> None:
    projects_dao.create_project(
        {
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
            "outline": json.dumps(_make_outline(chapter_count), ensure_ascii=False),
            "tags": "[]",
            "is_starred": 0,
            "premise": "测试",
            "target_chapters": chapter_count,
            "characters": "[]",
            "locations": "[]",
            "world_rules": "[]",
            "import_analysis": "null",
            "inspiration_fragments": "[]",
        }
    )


def _insert_chapter(chapter_id: str, title: str, order: int, content: str) -> None:
    chapters_dao.insert_chapter(
        {
            "id": chapter_id,
            "project_id": "proj-1",
            "volume_id": "v1",
            "title": title,
            "chapter_order": order,
            "content": content,
            "word_count": len(content.replace(" ", "")),
            "status": "completed",
            "summary": f"{title}摘要",
            "notes": "",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }
    )


def test_materialize_outline_chapters_creates_missing_rows_without_overwriting_existing(resume_db):
    """大纲章节可直接进入生成；已有正文绝不能被启动过程清空。"""
    _insert_project(chapter_count=3)
    _insert_chapter("c1", "第1章", 1, "已保存的第一章正文")
    outline_nodes = _make_outline(3)["nodes"]

    ngs._materialize_outline_chapters("proj-1", outline_nodes)

    rows = resume_db.execute(
        "SELECT id, volume_id, title, chapter_order, content, status FROM chapters ORDER BY chapter_order"
    ).fetchall()
    assert [row["id"] for row in rows] == ["c1", "c2", "c3"]
    assert rows[0]["content"] == "已保存的第一章正文"
    assert rows[1]["volume_id"] == "v1"
    assert rows[2]["title"] == "第3章"
    assert rows[1]["status"] == "draft"


def _insert_job(
    job_id: str = "job-1",
    *,
    status: str,
    mode: str = "all",
    total: int = 5,
    completed: int = 0,
    last_completed: str | None = None,
    pending: bool = False,
    resume_target: str = "",
    checkpoint_type: str = "",
    current_chapter_id: str | None = None,
) -> None:
    novel_jobs_dao.save_job(
        {
            "id": job_id,
            "project_id": "proj-1",
            "mode": mode,
            "requested_chapter_id": last_completed,
            "status": status,
            "message": "",
            "target_words": 10000,
            "total_chapters": total,
            "completed_chapters": completed,
            "failed_chapters": 0,
            "fallback_chapters": 0,
            "generated_words": 0,
            "current_chapter_id": current_chapter_id,
            "current_chapter_title": "",
            "last_completed_chapter_id": last_completed,
            "last_completed_chapter_title": "",
            "llm_provider": "",
            "llm_model": "",
            "last_generation_source": "",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "error": "",
            "cancel_requested": 0,
            "max_retries": 1,
            "retry_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "checkpoint_type": checkpoint_type,
            "resume_target_json": resume_target,
            "pending_manual_recovery": 1 if pending else 0,
            "heartbeat_at": "",
        }
    )


def _make_generate_mock(
    calls: dict,
    reqs: dict | None = None,
    *,
    fail_chapter_id: str | None = None,
):
    """生成函数 mock：按章节计数；可指定某章抛错模拟失败。"""

    async def fake_generate(req):
        calls[req.chapterId] = calls.get(req.chapterId, 0) + 1
        if reqs is not None:
            reqs[req.chapterId] = req
        if fail_chapter_id and req.chapterId == fail_chapter_id:
            raise RuntimeError(f"模拟 {req.chapterId} 生成失败")
        title = req.chapterTitle or req.chapterId
        text = (f"{title}正文片段。" * 400)[:2200]
        return {"text": text, "source": "llm", "provider": "mock", "model": "mock-model"}

    return fake_generate


# ══════════════════════════════════════════════════════
# 1. 重启后任务进入可恢复状态
# ══════════════════════════════════════════════════════

def test_restart_marks_running_job_recoverable(resume_db, memory_mocks):
    """重启恢复：queued/running → interrupted（可恢复），completed 不受影响。"""
    _insert_project()
    _insert_job("job-r", status="running", completed=3, total=5, last_completed="c3")
    _insert_job("job-q", status="queued", completed=0, total=5)
    _insert_job("job-done", status="completed", completed=5, total=5, last_completed="c5")

    count = ngs.recover_jobs_after_restart()
    assert count == 2

    running = ngs.get_job("job-r")
    assert running.status == "interrupted"
    assert running.is_recoverable is True
    assert running.to_payload()["recoverable"] is True
    # 保留 checkpoint：last_completed 不被清空
    assert running.last_completed_chapter_id == "c3"

    queued = ngs.get_job("job-q")
    assert queued.status == "interrupted"
    assert queued.is_recoverable is True

    done = ngs.get_job("job-done")
    assert done.status == "completed"
    assert done.is_recoverable is False


# ══════════════════════════════════════════════════════
# 2. resume 不重跑已生成章节（mock 计数验证）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_resume_does_not_regenerate_completed_chapters(resume_db, memory_mocks):
    """中断在 c3 后：恢复后只生成 c4/c5，c1-c3 不重跑。"""
    _insert_project()
    for i in range(1, 4):
        _insert_chapter(f"c{i}", f"第{i}章", i, f"第{i}章正文。" * 300)  # ~1800 字
    _insert_job("job-1", status="interrupted", completed=3, total=5, last_completed="c3")

    calls: dict[str, int] = {}
    with patch.object(ngs, "generate_continuation_result", new=_make_generate_mock(calls)):
        job = ngs.resume_job("job-1")
        await job.task

    assert job.status == "completed"
    assert calls.get("c1", 0) == 0
    assert calls.get("c2", 0) == 0
    assert calls.get("c3", 0) == 0
    assert calls.get("c4", 0) == 1
    assert calls.get("c5", 0) == 1
    assert job.completed_chapters == 5
    assert job.total_chapters == 5


# ══════════════════════════════════════════════════════
# 3. 恢复时前文记忆从 DB 读取
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_resume_reads_previous_chapter_memory_from_db(resume_db, memory_mocks):
    """恢复 c4 时，注入的上章结尾 / 章摘要均来自 DB 落库内容。"""
    _insert_project()
    _insert_chapter("c1", "第1章", 1, "第1章正文。" * 300)
    _insert_chapter("c2", "第2章", 2, "第2章正文。" * 300)
    c3_content = "第3章正文。" * 300 + "第三章节末独特标记"
    _insert_chapter("c3", "第3章", 3, c3_content)
    _insert_job("job-1", status="interrupted", completed=3, total=5, last_completed="c3")

    calls: dict[str, int] = {}
    reqs: dict[str, object] = {}
    with patch.object(ngs, "generate_continuation_result", new=_make_generate_mock(calls, reqs=reqs)):
        job = ngs.resume_job("job-1")
        await job.task

    req_c4 = reqs.get("c4")
    assert req_c4 is not None
    context_before = req_c4.contextBefore
    # 上一章结尾（c3 正文末尾）从 DB 读取
    assert "上一章结尾" in context_before
    assert "第三章节末独特标记" in context_before
    # 最近章节摘要（c3 摘要）从 DB 读取
    assert "第3章摘要" in context_before
    assert job.status == "completed"


# ══════════════════════════════════════════════════════
# 4. 失败章 checkpoint + 恢复时跳过
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_failed_chapter_default_retries_failed_chapter_on_resume(resume_db, memory_mocks):
    """P0/P1（Codex 复测）：c2 失败标记 pending_manual_recovery；恢复**默认重试失败章**（不跳过）。"""
    _insert_project()
    calls: dict[str, int] = {}

    with patch.object(
        ngs, "generate_continuation_result",
        new=_make_generate_mock(calls, fail_chapter_id="c2"),
    ):
        job = ngs.start_generation_job("proj-1", mode="all", max_retries=1)
        await job.task

    failed = ngs.get_job(job.id)
    assert failed.status == "failed"
    assert failed.pending_manual_recovery is True
    assert failed.checkpoint_type == "chapter_failed"
    assert "c2" in failed.resume_target_json
    assert calls.get("c1", 0) == 1
    assert calls.get("c2", 0) == 2  # 首次 + 1 次重试后放弃

    # 恢复：默认重试失败章 c2（不跳过），成功后继续 c3/c4/c5
    with patch.object(ngs, "generate_continuation_result", new=_make_generate_mock(calls)):
        resumed = ngs.resume_job(job.id)
        await resumed.task

    assert resumed.status == "completed"
    assert calls.get("c2", 0) == 3  # 失败章被重新生成（重试成功）
    assert calls.get("c3", 0) == 1
    assert calls.get("c4", 0) == 1
    assert calls.get("c5", 0) == 1
    assert resumed.failed_chapters == 1
    assert resumed.completed_chapters == 5  # 计数不重复（c1+c2重试成功+c3+c4+c5）


@pytest.mark.asyncio
async def test_resume_skip_failed_chapter_continues_from_next(resume_db, memory_mocks):
    """P0/P1：仅当显式 skip_failed_chapter=True（用户明确跳过）才跳过失败章，从下一章继续。"""
    _insert_project()
    calls: dict[str, int] = {}

    with patch.object(
        ngs, "generate_continuation_result",
        new=_make_generate_mock(calls, fail_chapter_id="c2"),
    ):
        job = ngs.start_generation_job("proj-1", mode="all", max_retries=1)
        await job.task

    assert ngs.get_job(job.id).status == "failed"
    with patch.object(ngs, "generate_continuation_result", new=_make_generate_mock(calls)):
        resumed = ngs.resume_job(job.id, skip_failed_chapter=True)
        await resumed.task

    assert resumed.status == "completed"
    assert calls.get("c2", 0) == 2  # 失败章未被重新生成（显式跳过）
    assert calls.get("c3", 0) == 1
    assert calls.get("c4", 0) == 1
    assert calls.get("c5", 0) == 1
    assert resumed.completed_chapters == 4  # c1 + c3 + c4 + c5（c2 被跳过）


@pytest.mark.asyncio
async def test_last_chapter_failed_default_retry(resume_db, memory_mocks):
    """P0/P1：末章失败**默认可重试失败章本身**（不再因无后继而被拒绝）。"""
    _insert_project(chapter_count=3)
    calls: dict[str, int] = {}

    with patch.object(
        ngs, "generate_continuation_result",
        new=_make_generate_mock(calls, fail_chapter_id="c3"),
    ):
        job = ngs.start_generation_job("proj-1", mode="all", max_retries=1)
        await job.task

    failed = ngs.get_job(job.id)
    assert failed.status == "failed"
    assert failed.pending_manual_recovery is True
    # 末章失败也应判为可恢复（默认重试失败章）
    assert failed.to_payload()["recoverable"] is True

    with patch.object(ngs, "generate_continuation_result", new=_make_generate_mock(calls)):
        resumed = ngs.resume_job(job.id)  # 默认重试末章 c3
        await resumed.task

    assert resumed.status == "completed"
    assert calls.get("c3", 0) == 3  # 末章被重试成功（首+重试+恢复重试）
    assert resumed.completed_chapters == 3


@pytest.mark.asyncio
async def test_last_chapter_failed_skip_rejected(resume_db, memory_mocks):
    """P0/P1：末章失败 + 显式 skip_failed_chapter=True → 拒绝（无法跳过，须重试）。"""
    _insert_project(chapter_count=3)
    calls: dict[str, int] = {}

    with patch.object(
        ngs, "generate_continuation_result",
        new=_make_generate_mock(calls, fail_chapter_id="c3"),
    ):
        job = ngs.start_generation_job("proj-1", mode="all", max_retries=1)
        await job.task

    assert ngs.get_job(job.id).status == "failed"
    with pytest.raises(RuntimeError, match="无法跳过"):
        ngs.resume_job(job.id, skip_failed_chapter=True)


@pytest.mark.asyncio
async def test_retry_failed_chapter_again_fails_stays_recoverable(resume_db, memory_mocks):
    """P0/P1：重试失败章再次失败 → 任务再次 failed+pending，仍可再次恢复重试（不丢 checkpoint）。"""
    _insert_project()
    calls: dict[str, int] = {}

    # c2 始终失败（start + resume 都失败）
    with patch.object(
        ngs, "generate_continuation_result",
        new=_make_generate_mock(calls, fail_chapter_id="c2"),
    ):
        job = ngs.start_generation_job("proj-1", mode="all", max_retries=1)
        await job.task
        assert ngs.get_job(job.id).status == "failed"

        # 恢复（默认重试 c2）→ 再次失败
        resumed = ngs.resume_job(job.id)
        await resumed.task

    again = ngs.get_job(job.id)
    assert again.status == "failed"
    assert again.pending_manual_recovery is True
    assert again.checkpoint_type == "chapter_failed"
    assert "c2" in again.resume_target_json
    # 仍可再次恢复重试
    assert again.to_payload()["recoverable"] is True


# ══════════════════════════════════════════════════════
# 5. 不可恢复场景拒绝
# ══════════════════════════════════════════════════════

def test_resume_rejects_completed_job(resume_db, memory_mocks):
    _insert_project()
    _insert_job("job-done", status="completed", completed=5, total=5, last_completed="c5")
    with pytest.raises(RuntimeError):
        ngs.resume_job("job-done")


def test_resume_rejects_all_chapters_generated(resume_db, memory_mocks):
    """interrupted 但章节实际已全部生成：拒绝恢复。"""
    _insert_project()
    for i in range(1, 6):
        _insert_chapter(f"c{i}", f"第{i}章", i, f"第{i}章正文。" * 300)
    _insert_job("job-x", status="interrupted", completed=3, total=5, last_completed="c3")
    with pytest.raises(RuntimeError):
        ngs.resume_job("job-x")


def test_resume_rejects_when_other_running_job_exists(resume_db, memory_mocks):
    """项目下已有运行中任务时拒绝恢复。"""
    _insert_project()
    _insert_job("job-a", status="interrupted", completed=2, total=5, last_completed="c2")
    _insert_job("job-b", status="running", completed=0, total=5)
    with pytest.raises(RuntimeError):
        ngs.resume_job("job-a")


@pytest.mark.asyncio
async def test_resume_last_chapter_failed_default_retries(resume_db, memory_mocks):
    """P0/P1：末章失败默认可重试失败章本身（不再抛"无可继续章节"）。"""
    _insert_project(chapter_count=5)
    _insert_job(
        "job-last",
        status="failed",
        completed=4,
        total=5,
        last_completed="c4",
        pending=True,
        resume_target=json.dumps({"chapter_id": "c5", "chapter_title": "第5章"}, ensure_ascii=False),
        checkpoint_type="chapter_failed",
        current_chapter_id="c5",
    )
    # 默认重试末章 c5：不再拒绝
    calls: dict[str, int] = {}
    with patch.object(ngs, "generate_continuation_result", new=_make_generate_mock(calls)):
        job = ngs.resume_job("job-last")
        assert job.requested_chapter_id == "c5"
        assert job.mode == "current-to-end"
        await job.task
    assert ngs.get_job("job-last").status == "completed"


def test_resume_last_chapter_failed_skip_rejected_sync(resume_db, memory_mocks):
    """P0/P1：末章失败 + 显式 skip_failed_chapter=True → 拒绝（无法跳过，须重试）。"""
    _insert_project(chapter_count=5)
    _insert_job(
        "job-last2",
        status="failed",
        completed=4,
        total=5,
        last_completed="c4",
        pending=True,
        resume_target=json.dumps({"chapter_id": "c5", "chapter_title": "第5章"}, ensure_ascii=False),
        checkpoint_type="chapter_failed",
        current_chapter_id="c5",
    )
    with pytest.raises(RuntimeError, match="无法跳过"):
        ngs.resume_job("job-last2", skip_failed_chapter=True)


def test_list_recoverable_jobs_filters_only_recoverable(resume_db, memory_mocks):
    _insert_project()
    _insert_job("job-interrupted", status="interrupted", completed=2, total=5, last_completed="c2")
    _insert_job("job-failed", status="failed", completed=1, total=5, last_completed="c1",
                pending=True, checkpoint_type="chapter_failed")
    _insert_job("job-done", status="completed", completed=5, total=5, last_completed="c5")
    _insert_job("job-cancelled", status="cancelled", completed=1, total=5)

    jobs = ngs.list_recoverable_jobs("proj-1")
    ids = [job.id for job in jobs]
    assert "job-interrupted" in ids
    assert "job-failed" in ids
    assert "job-done" not in ids
    assert "job-cancelled" not in ids


def test_list_recoverable_jobs_ignores_failed_job_when_target_chapter_later_completed(
    resume_db, memory_mocks,
):
    """A stale failure must not invite a paid retry after that chapter was later saved."""
    _insert_project()
    _insert_chapter("c3", "第3章", 3, "已完成正文" * 500)
    _insert_job(
        "job-stale-failure",
        status="failed",
        completed=0,
        total=1,
        pending=True,
        resume_target=json.dumps({"chapter_id": "c3", "chapter_title": "第3章"}),
        last_completed=None,
    )

    ids = [job.id for job in ngs.list_recoverable_jobs("proj-1")]

    assert "job-stale-failure" not in ids


def test_failed_last_chapter_listed_as_recoverable(resume_db, memory_mocks):
    """P0/P1：末章失败任务也进入可恢复列表（默认可重试失败章本身）。"""
    _insert_project()
    # 末章（c5）失败：默认可重试末章 → 应可恢复
    _insert_job(
        "job-last",
        status="failed",
        completed=4,
        total=5,
        last_completed="c4",
        pending=True,
        resume_target=json.dumps({"chapter_id": "c5", "chapter_title": "第5章"}, ensure_ascii=False),
        checkpoint_type="chapter_failed",
        current_chapter_id="c5",
    )
    # 非末章（c2）失败：同样可恢复
    _insert_job(
        "job-mid",
        status="failed",
        completed=1,
        total=5,
        last_completed="c1",
        pending=True,
        resume_target=json.dumps({"chapter_id": "c2", "chapter_title": "第2章"}, ensure_ascii=False),
        checkpoint_type="chapter_failed",
        current_chapter_id="c2",
    )

    last = ngs.get_job("job-last")
    mid = ngs.get_job("job-mid")
    assert last.to_payload()["recoverable"] is True
    assert mid.to_payload()["recoverable"] is True

    ids = [job.id for job in ngs.list_recoverable_jobs("proj-1")]
    assert "job-last" in ids
    assert "job-mid" in ids


# ══════════════════════════════════════════════════════
# 8. Codex P1-1：fallback 稳定性回归测试
# ══════════════════════════════════════════════════════

def test_start_job_rejected_when_llm_unconfigured(resume_db, memory_mocks):
    """Codex P1-1：LLM 未配置时，整书创作启动被明确拒绝（而非每章 fallback 失败空转 0 章落库）。"""
    _insert_project()
    with patch.object(ngs.llm, "is_configured", return_value=False):
        with pytest.raises(RuntimeError, match="AI 服务未配置"):
            ngs.start_generation_job("proj-1")


@pytest.mark.asyncio
async def test_fallback_does_not_pollute_chapter_and_job_fails(resume_db, memory_mocks):
    """P0-2：LLM 3 次全失败（source="failed"）时，fallback 不得污染正式章节。

    验证：
    - job 进入 failed + pending_manual_recovery（chapter_failed checkpoint）
    - 章节正文不落库（content 为空/无新内容）
    - completed_chapters 不增、项目字数不增
    - fallback 只作预览（preview 字段），绝不写正式章节
    """
    _insert_project()
    # 记录失败前项目字数
    conn = ngs.get_conn()
    before_total = conn.execute(
        "SELECT COALESCE(SUM(word_count),0) AS t FROM chapters WHERE project_id='proj-1'"
    ).fetchone()["t"]

    async def fake_failed_generate(req):
        # P0-2：generate_continuation_result 3 次失败 → text 空、source="failed"、preview 仅供 UI
        return {
            "text": "",
            "source": "failed",
            "error": "mock LLM 全失败",
            "preview": "本地降级预览文本（仅供 UI，不落库）。",
            "provider": "",
            "model": "",
        }

    with patch.object(ngs.llm, "is_configured", return_value=True), \
         patch.object(ngs, "generate_continuation_result", new=fake_failed_generate):
        job = ngs.start_generation_job("proj-1")
        await job.task

    # 任务失败，标记待人工处理（chapter_failed checkpoint）
    assert job.status == "failed"
    assert job.pending_manual_recovery is True
    assert job.checkpoint_type == "chapter_failed"
    assert job.completed_chapters == 0
    assert job.fallback_chapters == 0
    # 章节正文未落库（fallback 预览不写正式章节）
    conn = ngs.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chapters WHERE content != '' AND project_id='proj-1'"
    ).fetchone()
    assert row["n"] == 0
    # 项目字数未变
    after_total = conn.execute(
        "SELECT COALESCE(SUM(word_count),0) AS t FROM chapters WHERE project_id='proj-1'"
    ).fetchone()["t"]
    assert after_total == before_total
