"""Codex 复测 P1：失败章节恢复 —— 真实 API 集成测试（HTTP 层 + 临时 DB）。

与 test_novel_job_resume.py（单元级，直接调 resume_job）互补：
本文件通过 FastAPI TestClient 走真实 HTTP 路由 + 触发 lifespan（模拟后端重启），
验证 Codex 要求的验收点：

- 后端重启后 running → interrupted（recoverable=true），恢复后不重跑已完成章节
- 数据库中断后的恢复（重启即 DB 状态持久，内存运行时清空）
- 失败章正文为空时的重试（默认重试失败章本身）
- 失败章已有部分流式内容时的处理（first-unfinished 按字数阈值判定）
- SSE/REST 状态一致（POST resume 响应与 GET /{job_id} 一致）
- 取消、失败、恢复之间不互相覆盖（completed_chapters 等进度字段保留）
- 末章失败：默认重试成功；skip_failed_chapter=true 返回 400

说明：LLM 生成任务通过 patch ngs._run_generation_job 为 no-op 阻止真实调用；
本测试验证的是 HTTP 层 + 状态机决策，不依赖真实 LLM。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app import db as app_db
from app.dao import chapters as chapters_dao
from app.dao import novel_jobs as novel_jobs_dao
from app.dao import projects as projects_dao
from app.services import novel_generation_service as ngs
from main import app


# ══════════════════════════════════════════════════════
# Fixtures / Helpers
# ══════════════════════════════════════════════════════

@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """临时 DB + 清空运行时。TestClient lifespan 会 init_db + recover_jobs_after_restart。"""
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "api_resume.db")
    app_db.init_db()
    ngs._jobs.clear()
    ngs._job_subscribers.clear()
    yield
    ngs._jobs.clear()
    ngs._job_subscribers.clear()


def _make_outline(chapter_count: int = 5) -> dict:
    nodes = [{"id": "v1", "type": "volume", "title": "卷一", "order": 1}]
    for i in range(1, chapter_count + 1):
        nodes.append({
            "id": f"c{i}", "type": "chapter", "title": f"第{i}章", "order": i,
            "parentId": "v1", "summary": f"第{i}章摘要", "targetWordCount": 2000,
        })
    return {"mainSummary": "测试大纲", "nodes": nodes}


def _insert_project(project_id: str = "proj-1", chapter_count: int = 5) -> None:
    projects_dao.create_project({
        "id": project_id, "name": "API恢复测试", "subtitle": "", "genre": "xuanhuan",
        "target_word_count": 300000, "current_word_count": 0, "status": "planning",
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
        "style_config": "{}",
        "outline": json.dumps(_make_outline(chapter_count), ensure_ascii=False),
        "tags": "[]", "is_starred": 0, "premise": "测试", "target_chapters": chapter_count,
        "characters": "[]", "locations": "[]", "world_rules": "[]",
        "import_analysis": "null", "inspiration_fragments": "[]",
    })


def _insert_chapter(chapter_id: str, content: str, status: str = "completed", order: int = 1) -> None:
    chapters_dao.insert_chapter({
        "id": chapter_id, "project_id": "proj-1", "volume_id": "v1",
        "title": f"第{order}章", "chapter_order": order, "content": content,
        "word_count": len(content.replace(" ", "")), "status": status,
        "summary": "", "notes": "", "created_at": "2026-01-01", "updated_at": "2026-01-01",
    })


def _insert_job(
    job_id: str = "job-1",
    *,
    project_id: str = "proj-1",
    status: str,
    mode: str = "all",
    total: int = 5,
    completed: int = 0,
    pending: bool = False,
    resume_target: str = "",
    last_completed: str | None = None,
) -> None:
    novel_jobs_dao.save_job({
        "id": job_id, "project_id": project_id, "mode": mode,
        "requested_chapter_id": last_completed, "status": status, "message": "",
        "target_words": 10000, "total_chapters": total, "completed_chapters": completed,
        "failed_chapters": 0, "fallback_chapters": 0, "generated_words": 0,
        "current_chapter_id": None, "current_chapter_title": "",
        "last_completed_chapter_id": last_completed, "last_completed_chapter_title": "",
        "llm_provider": "", "llm_model": "", "last_generation_source": "",
        "started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "finished_at": None, "error": "", "cancel_requested": 0,
        "max_retries": 1, "retry_count": 0, "created_at": "2026-01-01T00:00:00Z",
        "checkpoint_type": "manual_recovery" if pending else "",
        "resume_target_json": resume_target,
        "pending_manual_recovery": 1 if pending else 0,
        "heartbeat_at": "",
    })


def _noop_run(job_id: str):
    """no-op 生成任务：阻止真实 LLM 调用（async def，可被 create_task 调度）。"""
    async def _inner():
        return None
    return _inner()


# ══════════════════════════════════════════════════════
# 用例
# ══════════════════════════════════════════════════════

def test_api_restart_marks_running_interrupted_then_resume(api_env):
    """后端重启（TestClient lifespan 触发 recover）→ running 变 interrupted+recoverable；
    默认 resume → first-unfinished，不重跑已完成章。"""
    _insert_project()
    _insert_chapter("c1", "第1章正文" * 200, status="completed", order=1)  # 已生成
    _insert_job(status="running", completed=1, last_completed="c1")

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            # 重启后状态：interrupted + recoverable
            r = client.get("/api/ai/job-1")
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["status"] == "interrupted"
            assert data["recoverable"] is True

            # 默认恢复：普通中断分支 requested_chapter_id=None（起点运行时定位），
            # mode=first-unfinished；不重跑已完成章（first-unfinished 定位到 c2）
            resp = client.post("/api/ai/job-1/resume")
            assert resp.status_code == 200
            resumed = resp.json()["data"]
            assert resumed["status"] == "queued"
            assert resumed["mode"] == "first-unfinished"
            assert resumed["requested_chapter_id"] is None
            # 验证起点定位逻辑：c1 已落库完成 → 第一个未完成章为 c2
            outline = _make_outline(5)
            nodes = ngs._chapter_nodes_from_outline(outline)
            assert ngs._find_first_unfinished_chapter_id("proj-1", nodes) == "c2"


def test_api_resume_failed_chapter_retries_itself(api_env):
    """失败章（正文为空）默认重试失败章本身，而非跳过。"""
    _insert_project()
    _insert_chapter("c1", "第1章正文" * 200, status="completed", order=1)
    _insert_job(status="failed", pending=True,
                resume_target=json.dumps({"chapter_id": "c3"}), completed=2)

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            resp = client.post("/api/ai/job-1/resume")
            assert resp.status_code == 200
            resumed = resp.json()["data"]
            assert resumed["status"] == "queued"
            # 默认重试失败章本身 c3
            assert resumed["requested_chapter_id"] == "c3"
            assert resumed["mode"] == "current-to-end"
            # 失败章正文为空时，任务应从空开始重试（requested 指回 c3 而非跳过）
            assert resumed["pending_manual_recovery"] is False


def test_api_resume_failed_chapter_skip_to_next(api_env):
    """显式 skip_failed_chapter=true 时从失败章下一章继续。"""
    _insert_project()
    _insert_chapter("c1", "第1章正文" * 200, status="completed", order=1)
    _insert_job(status="failed", pending=True,
                resume_target=json.dumps({"chapter_id": "c3"}), completed=2)

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            resp = client.post("/api/ai/job-1/resume?skip_failed_chapter=true")
            assert resp.status_code == 200
            resumed = resp.json()["data"]
            assert resumed["requested_chapter_id"] == "c4"


def test_api_last_chapter_failed_default_retry_skip_400(api_env):
    """末章失败：默认重试（job-1，独立项目）；显式跳过返回 400（job-2，独立项目）。
    同项目并行任务会被'已有进行中任务'拦截（正确行为），故用两个项目隔离。"""
    _insert_project(project_id="proj-1", chapter_count=3)
    _insert_job(job_id="job-1", project_id="proj-1", status="failed", total=3, pending=True,
                resume_target=json.dumps({"chapter_id": "c3"}), completed=2)
    _insert_project(project_id="proj-2", chapter_count=3)
    _insert_job(job_id="job-2", project_id="proj-2", status="failed", total=3, pending=True,
                resume_target=json.dumps({"chapter_id": "c3"}), completed=2)

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            resp = client.post("/api/ai/job-1/resume")
            assert resp.status_code == 200
            assert resp.json()["data"]["requested_chapter_id"] == "c3"

            resp2 = client.post("/api/ai/job-2/resume?skip_failed_chapter=true")
            assert resp2.status_code == 400
            assert "最后一章" in resp2.json()["detail"]


def test_api_resume_preserves_progress_fields(api_env):
    """取消/失败/恢复之间不互相覆盖：resume 保留 completed/failed 等进度字段。"""
    _insert_project()
    _insert_chapter("c1", "第1章正文" * 200, status="completed", order=1)
    _insert_chapter("c2", "第2章正文" * 200, status="completed", order=2)
    _insert_job(status="failed", pending=True,
                resume_target=json.dumps({"chapter_id": "c3"}), completed=2,
                last_completed="c2")

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            resp = client.post("/api/ai/job-1/resume")
            assert resp.status_code == 200
            resumed = resp.json()["data"]
            # 进度不被重置
            assert resumed["completed_chapters"] == 2
            assert resumed["total_chapters"] == 5
            assert resumed["last_completed_chapter_id"] == "c2"
            # 恢复后清除失败章待处理标记
            assert resumed["pending_manual_recovery"] is False
            assert resumed["resume_target_json"] == ""


def test_api_sse_rest_consistency(api_env):
    """POST resume 响应与 GET /{job_id} 一致（SSE/REST 同源同状态）。"""
    _insert_project()
    _insert_job(status="failed", pending=True,
                resume_target=json.dumps({"chapter_id": "c3"}), completed=2)

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            resp = client.post("/api/ai/job-1/resume")
            resumed = resp.json()["data"]
            # REST 查询（恢复后任务已入 _jobs，get_job 返回内存态）
            r = client.get("/api/ai/job-1")
            fetched = r.json()["data"]
            for field in ("id", "status", "mode", "requested_chapter_id",
                          "completed_chapters", "total_chapters",
                          "pending_manual_recovery", "resume_target_json"):
                assert fetched[field] == resumed[field], field


def test_api_restart_with_partial_streaming_content(api_env):
    """数据库中断恢复：失败章已有部分流式内容（不足阈值）→ first-unfinished 仍重试该章；
    若内容已达完成阈值 → 视为已完成跳过。"""
    _insert_project()
    # c1 已完整生成；c2 只有少量部分流式内容（远低于阈值 2000*0.35=700）
    _insert_chapter("c1", "第1章正文" * 200, status="completed", order=1)
    _insert_chapter("c2", "流式片段", status="draft", order=2)
    _insert_job(status="running", completed=1, last_completed="c1")

    with TestClient(app) as client:
        with patch.object(ngs, "_run_generation_job", new=_noop_run):
            # 重启 → interrupted
            r = client.get("/api/ai/job-1")
            assert r.json()["data"]["status"] == "interrupted"
            # 部分流式内容不足阈值 → first-unfinished（运行时定位到 c2 重试）
            resp = client.post("/api/ai/job-1/resume")
            assert resp.status_code == 200
            resumed = resp.json()["data"]
            assert resumed["mode"] == "first-unfinished"
            assert resumed["requested_chapter_id"] is None
            # 直接验证起点定位：c2 内容不足阈值 → first-unfinished 重试 c2（非跳过）
            outline = _make_outline(5)
            nodes = ngs._chapter_nodes_from_outline(outline)
            assert ngs._find_first_unfinished_chapter_id("proj-1", nodes) == "c2"
            # 若 c2 内容已达阈值（如 2000 字）→ 视为完成，跳过至 c3
            chapters_dao.upsert_chapter(
                "c2", "proj-1",
                {"content": "完成内容" * 500, "status": "completed",
                 "title": "第2章", "chapter_order": 2},
                "2026-01-01",
            )
            assert ngs._find_first_unfinished_chapter_id("proj-1", nodes) == "c3"
