"""阶段E：章节提交记录 + 记忆投影状态与单项重放
（借鉴 Webnovel Writer「提交链 / 投影日志」思想，自研实现，非复制 GPL 代码）。

覆盖验收：
- 章节提交记录：正文哈希 + 上下文哈希 + audit_id + 模型来源 + 完成时间，同章幂等
- 记忆投影状态：章节摘要 / 事实 / 实体 / 卷摘要 / FTS 五路各自 done/failed/skipped
- 单项重放：某项投影失败只重跑该项，不触发其他投影（mock 计数验证）
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app import db as app_db
from app.dao import chapter_commits as cc_dao
from app.dao import chapters as chapters_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import novel_jobs as novel_jobs_dao
from app.dao import projects as projects_dao
from app.services import memory_service
from app.services import novel_generation_service as ngs


@pytest.fixture
def commit_db(tmp_path, monkeypatch):
    """临时 DB：patch app.db.DB_PATH 后 init_db（含阶段E 两张新表）。"""
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "commit.db")
    app_db.init_db()
    yield


def _insert_project(pid: str = "proj-1") -> None:
    projects_dao.create_project({
        "id": pid, "name": "提交链项目", "premise": "主角 的征途。",
        "target_word_count": 100000, "current_word_count": 0, "status": "writing",
        "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        "outline": json.dumps({
            "mainSummary": "主角 的征途。",
            "nodes": [
                {"id": "v1", "type": "volume", "title": "第一卷", "order": 1},
                {"id": "c1", "type": "chapter", "parentId": "v1", "title": "第一章 开端", "order": 1},
                {"id": "c2", "type": "chapter", "parentId": "v1", "title": "第二章 波折", "order": 2},
            ],
        }),
        "style_config": "{}", "world_rules": "[]",
    })
    chapters_dao.insert_chapter({
        "id": "c1", "project_id": pid, "volume_id": "v1", "title": "第一章 开端",
        "chapter_order": 1, "content": "", "word_count": 0, "status": "draft",
        "summary": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    })


def _insert_job(pid: str = "proj-1", job_id: str = "job-1") -> None:
    novel_jobs_dao.save_job({
        "id": job_id, "project_id": pid, "mode": "all",
        "requested_chapter_id": None, "status": "running", "message": "",
        "target_words": 2000, "total_chapters": 2, "completed_chapters": 0,
        "failed_chapters": 0, "fallback_chapters": 0, "generated_words": 0,
        "current_chapter_id": "c1", "current_chapter_title": "第一章 开端",
        "last_completed_chapter_id": None, "last_completed_chapter_title": "",
        "llm_provider": "", "llm_model": "", "last_generation_source": "",
        "started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "finished_at": None, "error": "", "cancel_requested": 0,
        "max_retries": 1, "retry_count": 0, "created_at": "2026-01-01T00:00:00Z",
        "checkpoint_type": "", "resume_target_json": "", "pending_manual_recovery": 0,
        "heartbeat_at": "",
    })


def _chapter() -> dict:
    return {"id": "c1", "type": "chapter", "order": 1, "parentId": "v1",
            "title": "第一章 开端", "summary": "本章摘要", "conflict": "冲突",
            "revelation": "揭示", "emotion": "紧张"}


# ══════════════════════════════════════════════════════

def test_tables_and_hash(commit_db):
    """阶段E：两表存在；哈希可复现且不同正文哈希不同。"""
    with app_db.get_conn() as conn:
        t1 = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_commits'"
        ).fetchone()
        t2 = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_projections'"
        ).fetchone()
        assert t1 and t2
    h1 = cc_dao.sha256_hex("同一段正文")
    h2 = cc_dao.sha256_hex("同一段正文")
    h3 = cc_dao.sha256_hex("不同正文")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 32


def test_record_commit_roundtrip(commit_db):
    """P1：章节提交为**不可变版本链**——同章重复落库是追加新版本，不覆盖；
    get_commit 返回最新一条（快照视图）。"""
    _insert_project()
    cc_dao.record_commit(
        project_id="proj-1", chapter_id="c1",
        content_hash=cc_dao.sha256_hex("正文v1"), context_hash="ctx-1",
        audit_id="aud-1", provider="deepseek", model="deepseek-chat",
    )
    cc_dao.record_commit(
        project_id="proj-1", chapter_id="c1",
        content_hash=cc_dao.sha256_hex("正文v2"), context_hash="ctx-2",
        audit_id="aud-2", provider="deepseek", model="deepseek-chat",
    )
    # 最新快照 = version 2
    commit = cc_dao.get_commit("proj-1", "c1")
    assert commit is not None
    assert commit["version"] == 2
    assert commit["content_hash"] == cc_dao.sha256_hex("正文v2")
    assert commit["context_hash"] == "ctx-2"
    assert commit["audit_id"] == "aud-2"
    # 历史保留两条（不可变，不覆盖）
    history = cc_dao.get_commit_history("proj-1", "c1")
    assert len(history) == 2
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["parent_commit_id"] == ""
    assert history[1]["parent_commit_id"] == history[0]["id"]
    assert history[1]["content_hash"] == cc_dao.sha256_hex("正文v2")


def test_projection_status_roundtrip(commit_db):
    """阶段E：五路投影状态 done/failed/skipped 记录与查询。"""
    _insert_project()
    cc_dao.set_projection(project_id="proj-1", chapter_id="c1",
                          projection_type="chapter_summary", status="done")
    cc_dao.set_projection(project_id="proj-1", chapter_id="c1",
                          projection_type="facts", status="failed", detail="模拟失败")
    cc_dao.set_projection(project_id="proj-1", chapter_id="c1",
                          projection_type="entities", status="done")
    cc_dao.set_projection(project_id="proj-1", chapter_id="c1",
                          projection_type="volume", status="skipped")
    cc_dao.set_projection(project_id="proj-1", chapter_id="c1",
                          projection_type="fts", status="done")
    rows = cc_dao.list_projections("proj-1", "c1")
    by_type = {r["projection_type"]: r["status"] for r in rows}
    assert by_type == {
        "chapter_summary": "done", "facts": "failed", "entities": "done",
        "volume": "skipped", "fts": "done",
    }
    failed = cc_dao.list_failed_projections("proj-1", "c1")
    assert [f["projection_type"] for f in failed] == ["facts"]


@pytest.mark.asyncio
async def test_persist_commit_same_transaction_rollback(commit_db):
    """P1：正文落库与 commit 记录在同一事务——commit 写入失败则正文一并回滚（阻断性）。"""
    _insert_project()
    _insert_job()
    job = ngs.get_job("job-1")

    async def _fake_summarize(content, chapter_title=""):
        return "摘要"
    # commit 表缺失 → record_commit_on_conn 抛错 → _persist_generated_chapter 抛错
    with patch.object(memory_service, "summarize_chapter",
                      new=AsyncMock(side_effect=_fake_summarize)), \
         patch.object(memory_service, "extract_facts", new=AsyncMock(return_value=[])), \
         patch.object(memory_service, "extract_entities", new=AsyncMock(return_value=0)), \
         patch.object(memory_service, "record_chapter_completed", new=AsyncMock()), \
         patch.object(memory_fts_dao, "sync_chapter_fts", return_value=True):
        # 先正常落库一条（建立表后回滚测试由 drop 模拟）——此处直接验证同事务：
        # 让 commit 写入抛错（临时重命名表），断言正文也未落库
        with app_db.get_conn() as conn:
            conn.execute("ALTER TABLE chapter_commit_history RENAME TO chapter_commit_history_tmp")
        try:
            with pytest.raises(Exception):
                await ngs._persist_chapter_and_sync_memory(
                    job=job, chapter=_chapter(),
                    result={"content": "第一章正文。", "used_fallback": False,
                            "provider": "deepseek", "model": "deepseek-chat",
                            "last_source": "llm", "audit_id": "a", "context_hash": "c"},
                    outline={"mainSummary": "x", "nodes": []},
                )
        finally:
            with app_db.get_conn() as conn:
                conn.execute("ALTER TABLE chapter_commit_history_tmp RENAME TO chapter_commit_history")
    # 正文未落库（同事务回滚）
    content = chapters_dao.get_chapter_content("proj-1", "c1")
    assert content == ""


@pytest.mark.asyncio
async def test_persist_writes_commit_and_projections(commit_db):
    """阶段E：落库章节时写入提交记录 + 五路投影全部 done。"""
    _insert_project()
    _insert_job()
    job = ngs.get_job("job-1")

    async def _fake_summarize(content, chapter_title=""):
        return "本章摘要：云破霄 揭开真相。"
    with patch.object(memory_service, "summarize_chapter",
                      new=AsyncMock(side_effect=_fake_summarize)), \
         patch.object(memory_service, "extract_facts",
                      new=AsyncMock(return_value=[{"content": "事实1"}])), \
         patch.object(memory_service, "extract_entities",
                      new=AsyncMock(return_value=1)), \
         patch.object(memory_service, "record_chapter_completed",
                      new=AsyncMock()), \
         patch.object(memory_fts_dao, "sync_chapter_fts",
                      return_value=True):
        summary = await ngs._persist_chapter_and_sync_memory(
            job=job, chapter=_chapter(),
            result={
                "content": "第一章正文：云破霄 启程。",
                "used_fallback": False, "provider": "deepseek",
                "model": "deepseek-chat", "last_source": "llm",
                "audit_id": "aud-9", "context_hash": "ctx-9",
            },
            outline={"mainSummary": "主角 的征途。", "nodes": []},
        )

    assert summary.startswith("本章摘要")
    commit = cc_dao.get_commit("proj-1", "c1")
    assert commit is not None
    # 标题「第一章 开端」用的是**中文数字**，不属于新的「第N章 章节名」形态，
    # 所以落库时不会补标题行 —— 正文与模型输出一致（老数据行为不变）。
    assert commit["content_hash"] == cc_dao.sha256_hex("第一章正文：云破霄 启程。")
    assert commit["context_hash"] == "ctx-9"
    assert commit["audit_id"] == "aud-9"
    assert commit["provider"] == "deepseek"
    by_type = {r["projection_type"]: r["status"]
               for r in cc_dao.list_projections("proj-1", "c1")}
    assert all(by_type.get(t) == "done" for t in cc_dao.PROJECTION_TYPES), by_type


@pytest.mark.asyncio
async def test_replay_failed_projection_only(commit_db):
    """Legacy retry names are adapters over the canonical commit projection."""
    _insert_project()
    chapters_dao.upsert_chapter("c1", "proj-1", {"content": "第一章正文内容。", "status": "completed"}, "2026-01-01")
    with app_db.get_conn() as conn:
        cc_dao.record_commit_on_conn(
            conn, project_id="proj-1", chapter_id="c1",
            content_hash=cc_dao.sha256_hex("第一章正文内容。"), context_hash="ctx",
            content_snapshot="第一章正文内容。", observed_facts=["门已打开"],
        )
    with patch.object(memory_service, "summarize_chapter",
                      new=AsyncMock()) as summarize, \
         patch.object(memory_service, "extract_facts",
                      new=AsyncMock()) as facts, \
         patch.object(memory_service, "extract_entities",
                      new=AsyncMock()) as entities, \
         patch.object(memory_service, "record_chapter_completed",
                      new=AsyncMock()) as volume, \
         patch.object(memory_fts_dao, "sync_chapter_fts", return_value=True):
        replay = await ngs.replay_memory_projection(
            project_id="proj-1", chapter_id="c1", projection_type="facts",
        )
        assert replay["status"] == "done"
        assert replay["projection_type"] == "facts"
        summarize.assert_not_awaited()
        facts.assert_not_awaited()
        entities.assert_not_awaited()
        volume.assert_not_awaited()
