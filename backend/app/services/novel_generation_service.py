"""Persistent long-form novel generation jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

from app.core.config import settings
from app.dao import novel_jobs as novel_jobs_dao
from app.dao import projects as projects_dao
from app.dao import chapters as chapters_dao
from app.dao import volumes as volumes_dao
from app.dao import chapter_commits as chapter_commits_dao
from app.dao import chapter_contracts_dao
from app.dao import chapter_hooks as chapter_hooks_dao
from app.dao import foreshadows as foreshadows_dao
from app.dao import memory_entities as memory_entities_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import memory_recall as memory_recall_dao
from app.dao import novel_facts as novel_facts_dao
from app.db import get_conn, row_to_project, utc_today
from app.llm.audit import clear_llm_audit_context, set_llm_audit_context
from app.llm.provider import llm
from app.models.schemas import ContinueWritingRequest, gen_id
from app.services import fact_check_service
from app.services import chapter_contract_service
from app.services import foreshadow_service

# 2026-10-22：此前 `_logger.warning(...)` 被引用但从未定义 —— 事实核查未通过时
# 会抛 NameError，又被外层 `except Exception: pass` 吞掉，日志永远不输出。
_logger = logging.getLogger(__name__)
from app.services import hook_service
from app.services import memory_service
from app.services import story_state_transition_service
from app.services import story_state_service
from app.services import character_voice_service
from app.services import writing_experience_service
from app.utils.chapter_heading import ensure_chapter_heading
from app.services import deterministic_qa_service
from app.services import review_service
from app.services import rewrite_service
from app.services.style_engine import profile_service as style_profile_service
from app.services.style_engine import transition_service as style_transition_service
from app.services.ai_service import generate_continuation_result, generate_continuation_stream_result
from app.services.chapter_budget_service import ChapterBudgetExceeded, ChapterGenerationBudget
from app.services import write_gate_service
from app.services import projection_runner_service
from app.services import reference_work_service
from app.services import scene_guidance_service
from app.services import user_quota_service
from app.utils.text_repair import repair_text
from app.utils.word_count import count_words


MIN_CHAPTER_WORDS = 1500
MIN_EXPANSION_WORDS = 600
MAX_EXPANSION_WORDS = 2400
EXPANSION_WORD_BUFFER = 300
MAX_EXPANSION_PASSES = 3
MIN_ACCEPTABLE_RATIO = 0.9
RUNNING_STATUSES = {"queued", "running"}
INTERRUPTIBLE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

# ── 单章自检 LLM 调用预算（Codex P1-2：调用成本过高） ──
# 长跑实测每章约 15 次 LLM 调用（正文 1 + 事实核查最多 9 + 钩子最多 5 + AI 味 1-2 +
# 文笔链最多 10）。为把单章调用压到可控范围：
#   - 事实核查：max_retries=1（1 核查 + 1 重写 + 1 根因 + 1 复检，最多 4 次）
#   - 钩子重写：MAX_HOOK_REWRITES 由调用方传 1（1 检测 + 1 重写 + 1 复检，最多 3 次）
#   - 文笔重写：max_rewrites=1（只对最低分 auto 维度重写一次）
#   - 自检总预算：MAX_SELF_CHECK_LLM_CALLS 兜底，超限跳过后续自检环节（不阻塞落库）
MAX_SELF_CHECK_LLM_CALLS = 8
MAX_SELF_CHECK_HOOK_REWRITES = 1

# Phase C: one auditable budget shared by all operations for one chapter.
MAX_GENERATION_CALLS = 4
MAX_RETRY_CALLS = 1
MAX_QA_CALLS = 8
MAX_REWRITE_CALLS = 1


class GenerationIncompleteError(RuntimeError):
    """The provider returned output that cannot be safely committed as a chapter."""


def _chapter_budget_for_job(job: "NovelGenerationJob", target_words: int) -> ChapterGenerationBudget:
    """Return the chapter-call budget for a generation job.

    ``single`` is the explicitly controlled, one-chapter flow used by the
    Beta editor.  It must not quietly expand into a retry, review, rewrite,
    or continuation chain after the user starts one generation.
    """
    if job.mode == "single":
        return ChapterGenerationBudget(
            max_generation_calls=1,
            max_retry_calls=0,
            max_qa_calls=0,
            max_rewrite_calls=0,
            target_words=target_words,
            acceptable_words=_minimum_acceptable_words(target_words),
            minimum_words=max(MIN_CHAPTER_WORDS, int(target_words * 0.75)),
        )
    return ChapterGenerationBudget(
        max_retry_calls=min(max(int(job.max_retries), 0), MAX_RETRY_CALLS),
        target_words=target_words,
        acceptable_words=_minimum_acceptable_words(target_words),
        minimum_words=max(MIN_CHAPTER_WORDS, int(target_words * 0.75)),
    )


def _effective_max_retries(mode: str, requested_retries: int) -> int:
    """Normalize client retry input without letting controlled mode expand scope."""
    if mode == "single":
        return 0
    return max(0, min(int(requested_retries), MAX_RETRY_CALLS))



# ── 上下文预算（记忆升级 v1：显式预算分配，替代下游 [-4000:] 硬截断） ──
# 各记忆分块按优先级分配字符预算，低优先级分块先被裁剪；
# milestones（已完成事实）与实体状态为最高优先级，永不因预算被整体丢弃。
CONTEXT_BUDGET_TOTAL = 7000      # contextBefore 组装后总预算上限（Codex P2 复测 P1：5200→7000 容纳更大事实账本）
CONTEXT_BUDGET_DOWNSTREAM_FALLBACK = 8200  # 下游 ai_service 最终兜底窗口（contextBefore[-8200:]）
CONTEXT_BUDGET_PREMISE = 200       # premise
CONTEXT_BUDGET_HEADER = 600        # 章节元数据 + 大纲/章摘要/冲突/揭示/情绪
CONTEXT_BUDGET_VOLUME = 3000       # 已完结卷摘要（5 卷 × 600 上限）
CONTEXT_BUDGET_RECENT = 1100       # 最近章节摘要（5 章 × 220 上限）
CONTEXT_BUDGET_ENDING = 800        # 上一章结尾
CONTEXT_BUDGET_ENTITY = 400        # 实体当前状态（最高优先级之一）
CONTEXT_BUDGET_FTS = 500           # 相关历史摘要（FTS 检索）
CONTEXT_BUDGET_MILESTONES = 3000   # 已完成事实（最高优先级，永不丢弃；Codex P2 复测 P1：1500→3000 确保早期关键事实不被挤出）
CONTEXT_BUDGET_FORESHADOW = 400    # 未回收伏笔（阶段C 新增，低优先级）
CONTEXT_BUDGET_TIMELINE = 300      # 时间线约束（阶段C 新增，低优先级）



def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


@dataclass
class NovelGenerationJob:
    id: str
    project_id: str
    mode: str
    requested_chapter_id: str | None
    status: str
    message: str
    target_words: int
    total_chapters: int
    completed_chapters: int = 0
    failed_chapters: int = 0
    fallback_chapters: int = 0
    generated_words: int = 0
    current_chapter_id: str | None = None
    current_chapter_title: str | None = None
    last_completed_chapter_id: str | None = None
    last_completed_chapter_title: str | None = None
    llm_provider: str = ""
    llm_model: str = ""
    last_generation_source: str = ""
    started_at: str = field(default_factory=_utc_iso)
    updated_at: str = field(default_factory=_utc_iso)
    finished_at: str | None = None
    error: str = ""
    cancel_requested: bool = False
    max_retries: int = 1
    retry_count: int = 0
    created_at: str = field(default_factory=_utc_iso)
    # M2 断点续传字段
    checkpoint_type: str = ""
    resume_target_json: str = ""
    pending_manual_recovery: bool = False
    heartbeat_at: str = ""
    # M3 自检闭环统计（随 checkpoint 持久化，前端据此展示质量指标）
    fact_check_count: int = 0
    rewrite_count: int = 0
    hook_miss_count: int = 0
    quality_debt_count: int = 0
    # 第 3 档动态路由：置信度升级链复查次数（低置信度 → 强模型复查，随 checkpoint 持久化）
    escalation_count: int = 0
    # 仅用于运行中的 SSE 预览，不落库为正式章节。
    preview_chapter_id: str | None = None
    preview_content: str = ""
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)

    @property
    def is_recoverable(self) -> bool:
        """任务是否处于可恢复状态（中断中 / 失败 / 已取消但尚有未完成章节）。"""
        if self.status not in {"interrupted", "failed", "cancelled"}:
            return False
        if self.total_chapters > 0 and self.completed_chapters >= self.total_chapters:
            return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "mode": self.mode,
            "requested_chapter_id": self.requested_chapter_id,
            "status": self.status,
            "message": self.message,
            "target_words": self.target_words,
            "total_chapters": self.total_chapters,
            "completed_chapters": self.completed_chapters,
            "failed_chapters": self.failed_chapters,
            "fallback_chapters": self.fallback_chapters,
            "generated_words": self.generated_words,
            "current_chapter_id": self.current_chapter_id,
            "current_chapter_title": self.current_chapter_title,
            "last_completed_chapter_id": self.last_completed_chapter_id,
            "last_completed_chapter_title": self.last_completed_chapter_title,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "last_generation_source": self.last_generation_source,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            # M2 断点续传信息（查询接口一并暴露，前端据此渲染"可恢复"状态）
            "checkpoint_type": self.checkpoint_type,
            "resume_target_json": self.resume_target_json,
            "pending_manual_recovery": self.pending_manual_recovery,
            "heartbeat_at": self.heartbeat_at,
            # M3 自检闭环统计
            "fact_check_count": self.fact_check_count,
            "rewrite_count": self.rewrite_count,
            "hook_miss_count": self.hook_miss_count,
            "quality_debt_count": self.quality_debt_count,
            # 第 3 档动态路由：置信度升级链复查次数
            "escalation_count": self.escalation_count,
            "preview_chapter_id": self.preview_chapter_id,
            "preview_content": self.preview_content,
            # 综合判断（含末章失败场景），见 _is_job_recoverable
            "recoverable": _is_job_recoverable(self),
        }


_jobs: dict[str, NovelGenerationJob] = {}
_job_subscribers: dict[str, set[asyncio.Queue[NovelGenerationJob]]] = {}


def _row_to_job(row: sqlite3.Row | dict[str, Any]) -> NovelGenerationJob:
    job = NovelGenerationJob(
        id=row["id"],
        project_id=row["project_id"],
        mode=row["mode"],
        requested_chapter_id=row["requested_chapter_id"],
        status=row["status"],
        message=repair_text(row["message"] or ""),
        target_words=int(row["target_words"] or 0),
        total_chapters=int(row["total_chapters"] or 0),
        completed_chapters=int(row["completed_chapters"] or 0),
        failed_chapters=int(row["failed_chapters"] or 0),
        fallback_chapters=int(row["fallback_chapters"] or 0),
        generated_words=int(row["generated_words"] or 0),
        current_chapter_id=row["current_chapter_id"],
        current_chapter_title=repair_text(row["current_chapter_title"] or "") or None,
        last_completed_chapter_id=row["last_completed_chapter_id"],
        last_completed_chapter_title=repair_text(row["last_completed_chapter_title"] or "") or None,
        llm_provider=repair_text(row["llm_provider"] or ""),
        llm_model=repair_text(row["llm_model"] or ""),
        last_generation_source=repair_text(row["last_generation_source"] or ""),
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        error=repair_text(row["error"] or ""),
        cancel_requested=bool(row["cancel_requested"]),
        max_retries=int(row["max_retries"]) if row["max_retries"] is not None else 1,
        retry_count=int(row["retry_count"] or 0),
        created_at=row["created_at"],
        checkpoint_type=repair_text(row["checkpoint_type"] or ""),
        resume_target_json=repair_text(row["resume_target_json"] or ""),
        pending_manual_recovery=bool(row["pending_manual_recovery"]),
        heartbeat_at=repair_text(row["heartbeat_at"] or ""),
        fact_check_count=int(row.get("fact_check_count") or 0),
        rewrite_count=int(row.get("rewrite_count") or 0),
        hook_miss_count=int(row.get("hook_miss_count") or 0),
        quality_debt_count=int(row.get("quality_debt_count") or 0),
        escalation_count=int(row.get("escalation_count") or 0),
    )
    if job.checkpoint_type == "style_plan_conflict":
        candidate = json.loads(job.resume_target_json or "{}")
        job.preview_content = candidate.get("candidateContent") or ""
        job.preview_chapter_id = candidate.get("chapter_id")
    return job


def _save_job(job: NovelGenerationJob) -> None:
    novel_jobs_dao.save_job({
        "id": job.id,
        "project_id": job.project_id,
        "mode": job.mode,
        "requested_chapter_id": job.requested_chapter_id,
        "status": job.status,
        "message": repair_text(job.message),
        "target_words": job.target_words,
        "total_chapters": job.total_chapters,
        "completed_chapters": job.completed_chapters,
        "failed_chapters": job.failed_chapters,
        "fallback_chapters": job.fallback_chapters,
        "generated_words": job.generated_words,
        "current_chapter_id": job.current_chapter_id,
        "current_chapter_title": repair_text(job.current_chapter_title or ""),
        "last_completed_chapter_id": job.last_completed_chapter_id,
        "last_completed_chapter_title": repair_text(job.last_completed_chapter_title or ""),
        "llm_provider": repair_text(job.llm_provider),
        "llm_model": repair_text(job.llm_model),
        "last_generation_source": repair_text(job.last_generation_source),
        "started_at": job.started_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
        "error": repair_text(job.error),
        "cancel_requested": 1 if job.cancel_requested else 0,
        "max_retries": job.max_retries,
        "retry_count": job.retry_count,
        "created_at": job.created_at,
        "checkpoint_type": repair_text(job.checkpoint_type),
        "resume_target_json": repair_text(job.resume_target_json),
        "pending_manual_recovery": 1 if job.pending_manual_recovery else 0,
        "heartbeat_at": repair_text(job.heartbeat_at),
        "fact_check_count": job.fact_check_count,
        "rewrite_count": job.rewrite_count,
        "hook_miss_count": job.hook_miss_count,
        "quality_debt_count": job.quality_debt_count,
        "escalation_count": job.escalation_count,
    })
    _publish_job_update(job)


def _publish_job_update(job: NovelGenerationJob) -> None:
    subscribers = _job_subscribers.get(job.id)
    if not subscribers:
        return

    stale_subscribers: list[asyncio.Queue[NovelGenerationJob]] = []
    for queue in list(subscribers):
        try:
            queue.put_nowait(job)
        except RuntimeError:
            stale_subscribers.append(queue)

    for queue in stale_subscribers:
        subscribers.discard(queue)

    if not subscribers:
        _job_subscribers.pop(job.id, None)


def _load_job(job_id: str) -> NovelGenerationJob | None:
    row_dict = novel_jobs_dao.get_job(job_id)
    return _row_to_job(row_dict) if row_dict else None


def _load_latest_project_job(project_id: str, *, running_only: bool = False) -> NovelGenerationJob | None:
    row_dict = novel_jobs_dao.get_latest_project_job(project_id, running_only=running_only)
    return _row_to_job(row_dict) if row_dict else None


def list_project_jobs(project_id: str, limit: int = 20) -> list[NovelGenerationJob]:
    rows = novel_jobs_dao.list_jobs(project_id, limit=limit)
    return [_row_to_job(row) for row in rows]


def get_job(job_id: str) -> NovelGenerationJob | None:
    runtime = _jobs.get(job_id)
    if runtime:
        return runtime
    return _load_job(job_id)


def get_project_running_job(project_id: str) -> NovelGenerationJob | None:
    job = _load_latest_project_job(project_id, running_only=True)
    if not job:
        return None
    runtime = _jobs.get(job.id)
    if runtime:
        return runtime
    return job


def get_project_latest_job(project_id: str) -> NovelGenerationJob | None:
    runtime = get_project_running_job(project_id)
    if runtime:
        return runtime
    return _load_latest_project_job(project_id)


async def stream_job_updates(job_id: str, keepalive_seconds: float = 15.0) -> AsyncIterator[NovelGenerationJob | None]:
    job = get_job(job_id)
    if not job:
        return

    queue: asyncio.Queue[NovelGenerationJob] = asyncio.Queue()
    subscribers = _job_subscribers.setdefault(job_id, set())
    subscribers.add(queue)

    try:
        queue.put_nowait(job)
        while True:
            try:
                update = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
            except TimeoutError:
                yield None
                continue

            yield update
            if update.status in TERMINAL_STATUSES:
                break
    finally:
        subscribers.discard(queue)
        if not subscribers:
            _job_subscribers.pop(job_id, None)


def recover_jobs_after_restart() -> int:
    """M2：后端重启后把所有进行中（queued/running）任务标记为 interrupted（可恢复）。

    与旧行为（直接标 cancelled）不同：保留已完成章节 checkpoint，任务可在重启后
    通过 resume_job 从最近断点继续，而非全部重跑或丢弃。
    """
    _jobs.clear()
    now = _utc_iso()
    message = "后端已重启，长篇创作任务已中断，可从上次完成章节继续。"
    count = novel_jobs_dao.mark_interrupted_jobs(message, now)
    return count


def list_recoverable_jobs(project_id: str, limit: int = 20) -> list[NovelGenerationJob]:
    """获取某项目处于可恢复状态（interrupted / 失败章待处理）的任务列表。

    QA P1：末章失败任务（failed + pending_manual_recovery 且失败章之后无章节）
    resume 必然被「失败章已是最后一章」守卫拒绝，此处一并过滤，避免前端展示
    误导性的"可恢复"入口。
    """
    rows = novel_jobs_dao.list_recoverable_jobs(project_id, limit=limit)
    jobs = [_row_to_job(row) for row in rows]
    return [job for job in jobs if _is_job_recoverable(job)]


def resume_job(job_id: str, skip_failed_chapter: bool = False) -> NovelGenerationJob:
    """M2：从断点恢复长篇创作任务，继续生成而非全部重跑。

    - 普通中断（interrupted，如后端重启）：基于章节落库内容自动跳过已生成章节，
      从第一个未完成章节继续（不重跑已生成章节）。
    - 失败章（failed + pending_manual_recovery）：**默认重试失败章本身**（不跳过），
      保证整书不因中间失败永久缺章；末章失败也能重试。
      仅当调用方显式传 skip_failed_chapter=True（用户明确选择跳过该章）时，
      才从失败章的下一章继续；末章失败无法跳过时会拒绝。
    - 状态不可恢复 / 任务仍在内存中运行 / 项目已有其它运行中任务：抛 RuntimeError。
    """
    job = _load_job(job_id)
    if not job:
        raise RuntimeError("长篇创作任务未找到。")
    if job.checkpoint_type == "style_plan_conflict":
        raise RuntimeError("风格计划已变化，旧候选已保留。请查看候选后显式发起新创作，不可自动恢复旧任务。")
    if job.id in _jobs:
        raise RuntimeError("该任务仍在运行中，无需恢复。")

    running = get_project_running_job(job.project_id)
    if running and running.id != job.id:
        raise RuntimeError("当前项目已有进行中的长篇创作任务。")

    if not job.is_recoverable:
        raise RuntimeError("该任务当前不可恢复（已完成或处于不可中断状态）。")

    project = _load_project(job.project_id)
    outline = project.get("outline")
    if not outline or not outline.get("nodes"):
        raise RuntimeError("当前项目还没有可用大纲，无法恢复整书创作。")
    chapter_nodes = _chapter_nodes_from_outline(outline)

    if job.pending_manual_recovery and job.resume_target_json:
        try:
            failed = json.loads(job.resume_target_json)
        except (TypeError, ValueError):
            failed = {}
        failed_id = failed.get("chapter_id") or ""
        if skip_failed_chapter:
            # 用户明确选择跳过失败章 → 从下一章继续（末章失败无法跳过）
            start_id = _chapter_id_after(chapter_nodes, failed_id) if failed_id else None
            if not start_id:
                raise RuntimeError("失败章已是最后一章，无法跳过；请改为重试失败章。")
        else:
            # 默认重试失败章本身（不跳过，保证整书不因中间失败永久缺章）
            start_id = failed_id or None
            if start_id and start_id not in {n["id"] for n in chapter_nodes}:
                # 失败章已不在大纲（大纲被改）：回退自动定位第一个未完成章节
                start_id = None
        if not start_id:
            job.requested_chapter_id = None
            job.mode = "first-unfinished"
            if not _find_first_unfinished_chapter_id(job.project_id, chapter_nodes):
                raise RuntimeError("无法定位恢复起点（失败章已不在大纲且无未完成章节）。")
        else:
            job.requested_chapter_id = start_id
            job.mode = "current-to-end"
    else:
        # 普通中断：基于 DB 内容自动定位第一个未完成章节，已生成章节不重跑
        job.requested_chapter_id = None
        job.mode = "first-unfinished"
        if not _find_first_unfinished_chapter_id(job.project_id, chapter_nodes):
            raise RuntimeError("所有章节均已生成完毕，无需恢复。")

    now = _utc_iso()
    job.status = "queued"
    job.message = "已从断点恢复整书创作。"
    job.error = ""
    job.finished_at = None
    job.cancel_requested = False
    job.checkpoint_type = ""
    job.resume_target_json = ""
    job.pending_manual_recovery = False
    job.updated_at = now
    _save_job(job)

    job.task = asyncio.create_task(_run_generation_job(job.id))
    _jobs[job.id] = job
    return job


def request_cancel(job_id: str) -> NovelGenerationJob | None:
    job = get_job(job_id)
    if not job:
        return None

    runtime = _jobs.get(job_id)
    if runtime:
        job = runtime

    job.cancel_requested = True
    job.updated_at = _utc_iso()
    job.message = "已收到停止请求，将在当前章节完成后结束。"

    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = _utc_iso()

    _save_job(job)
    return job


def cancel_job_and_wait(job_id: str, timeout_seconds: int = 10) -> NovelGenerationJob | None:
    """取消任务并轮询等待进入终态；超时后强制标记 cancelled，不阻塞调用方。

    Codex P1：消除"停止后立刻删除项目返回 409"的竞态。
    """
    import time

    job = request_cancel(job_id)
    if not job:
        return None

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        # 重新加载——任务循环可能已更新状态
        job = get_job(job_id)
        if job and job.status in TERMINAL_STATUSES:
            return job
        time.sleep(0.5)

    # 超时：强制标记终态（任务循环会在下次 cancel_requested 检查点退出）
    job = get_job(job_id)
    if job and job.status not in TERMINAL_STATUSES:
        job.cancel_requested = True
        job.status = "cancelled"
        job.finished_at = _utc_iso()
        job.message = "强制停止（超时未完成当前章节）。"
        _save_job(job)
        # 清空内存 runtime 引用，避免后续任务循环覆盖数据库终态
        _jobs.pop(job_id, None)
    return job


def _apply_user_quota(chapter_nodes: list[dict[str, Any]], user_id: str) -> tuple[list[dict[str, Any]], str]:
    """按「非管理员每日自动创作章数」额度裁剪本次任务。

    返回 (裁剪后的章节节点, 提示文案)。额度用尽时抛 RuntimeError，由 API 层转成
    带业务文案的 400；管理员或未启用额度时原样返回且不带提示。
    """
    remaining = user_quota_service.remaining_chapters(user_id)
    if remaining is None:
        return list(chapter_nodes), ""
    if remaining <= 0:
        raise RuntimeError(user_quota_service.quota_exhausted_message())
    if len(chapter_nodes) > remaining:
        return (
            list(user_quota_service.clamp_to_quota(chapter_nodes, remaining)),
            user_quota_service.quota_partial_message(remaining),
        )
    return list(chapter_nodes), ""


def start_generation_job(
    project_id: str,
    mode: str = "all",
    current_chapter_id: str | None = None,
    max_retries: int = 1,
) -> NovelGenerationJob:
    running = get_project_running_job(project_id)
    if running:
        raise RuntimeError("当前项目已有进行中的长篇创作任务。")

    # P1-1：LLM 未配置时明确拒绝启动整书任务（避免每章 fallback 失败空转、0 章落库）
    if not llm.is_configured():
        raise RuntimeError("AI 服务未配置，无法启动整书创作。请先在「模型设置」中配置 API Key。")

    project = _load_project(project_id)
    outline = project.get("outline")
    if not outline or not outline.get("nodes"):
        raise RuntimeError("当前项目还没有可用大纲，无法启动整书创作。")

    chapter_nodes = _chapter_nodes_from_outline(outline)
    resolved_start_chapter_id = _resolve_start_chapter_id(
        project_id=project_id,
        chapter_nodes=chapter_nodes,
        mode=mode,
        requested_chapter_id=current_chapter_id,
    )
    selected_nodes = _select_chapter_nodes(
        project_id=project_id,
        chapter_nodes=chapter_nodes,
        mode=mode,
        current_chapter_id=resolved_start_chapter_id,
    )
    if not selected_nodes:
        raise RuntimeError("当前模式下没有需要创作的章节。")

    # 产品层额度：非管理员每天最多自动创作 N 章；改写/审校/续写不占用额度。
    # 启动时按**剩余额度裁剪本次任务**，而不是写到一半才撞额度——这样任务状态机
    # 不需要新增分支，前端进度条也始终自洽（total_chapters 就是本次真正会写的章数）。
    quota_user_id = projects_dao.get_project_owner(project_id) or ""
    selected_nodes, quota_notice = _apply_user_quota(selected_nodes, quota_user_id)

    now = _utc_iso()
    job = NovelGenerationJob(
        id=gen_id(),
        project_id=project_id,
        mode=mode,
        requested_chapter_id=resolved_start_chapter_id,
        status="queued",
        message=quota_notice + (
            f"准备从《{selected_nodes[0]['title']}》开始继续整书创作。"
            if mode == "first-unfinished" and resolved_start_chapter_id
            else "整书创作任务已排队。"
        ),
        target_words=project.get("targetWordCount") or 0,
        total_chapters=len(selected_nodes),
        max_retries=_effective_max_retries(mode, max_retries),
        started_at=now,
        updated_at=now,
        created_at=now,
    )
    _save_job(job)
    job.task = asyncio.create_task(_run_generation_job(job.id))
    _jobs[job.id] = job
    return job


async def _run_generation_job(job_id: str) -> None:
    runtime_job = _jobs.get(job_id)
    job = runtime_job or _load_job(job_id)
    if not job:
        return

    job.status = "running"
    job.message = "整书创作任务已启动。"
    job.updated_at = _utc_iso()
    _save_job(job)

    # Codex 六轮 P1：整书任务全程设置 LLM 审计上下文（project_id / job_id），
    # 该任务内所有真实 LLM 调用都会带上 job 关联，E2E 报告可精确关联证据。
    set_llm_audit_context(project_id=job.project_id, job_id=job.id)
    try:
        project = _load_project(job.project_id)
        outline = project["outline"]
        full_chapter_nodes = _chapter_nodes_from_outline(outline)
        chapter_nodes = _select_chapter_nodes(
            project_id=job.project_id,
            chapter_nodes=full_chapter_nodes,
            mode=job.mode,
            current_chapter_id=job.requested_chapter_id,
        )
        premise = project.get("premise") or project.get("subtitle") or ""
        characters = project.get("characters") or []
        world_rules = project.get("worldRules") or []
        style_config = project.get("styleConfig") or {}
        outline_summary = outline.get("mainSummary", "")

        previous_summary = _load_previous_summary(job.project_id, full_chapter_nodes, job.requested_chapter_id)
        last_context_summary = ""
        # M1：预建卷记录（幂等；项目无卷概念时按每 10 章切分）
        memory_service.ensure_volumes(job.project_id, outline)
        # 产品层额度只对非管理员生效；管理员默认不限额，因此这里一次性判定，
        # 避免每章重复查询账号表。
        quota_user_id = projects_dao.get_project_owner(job.project_id) or ""
        quota_tracked = bool(quota_user_id) and not user_quota_service.is_unlimited(quota_user_id)
        for index, chapter in enumerate(chapter_nodes):
            # 额度硬保护：同日并发启动另一个项目任务时，额度可能已被对方用光。
            # 此时正常收尾（已完成章节都已落库），而不是抛错让用户以为生成坏了。
            if quota_tracked and user_quota_service.remaining_chapters(quota_user_id) == 0:
                job.status = "completed"
                job.message = user_quota_service.quota_exhausted_message()
                job.finished_at = _utc_iso()
                job.updated_at = _utc_iso()
                _save_job(job)
                break
            if job.cancel_requested:
                job.status = "cancelled"
                job.message = "整书创作已取消。"
                job.finished_at = _utc_iso()
                job.updated_at = _utc_iso()
                _save_job(job)
                break

            # Chapter Contract and commit flows require the current outline
            # node to have a durable chapter row.  Materialize one chapter at
            # a time so future empty rows never enter continuity checks.
            _materialize_outline_chapters(job.project_id, [chapter])

            job.current_chapter_id = chapter["id"]
            job.current_chapter_title = chapter["title"]
            job.message = f"正在创作《{chapter['title']}》({index + 1}/{len(chapter_nodes)})"
            job.updated_at = _utc_iso()
            _save_job(job)

            # M1：构建分层记忆上下文（已完结卷摘要 + 近 5 章摘要 + 上一章结尾 + 已完成事实）
            memory_context = _build_memory_context(
                project_id=job.project_id,
                chapter_nodes=full_chapter_nodes,
                current_chapter_id=chapter["id"],
            )
            # M4：每章重建风格契约（P1-2：过渡进度每章落库推进后，注入的过渡权重随章节真实变化）
            style_contract = style_profile_service.build_generation_contract(job.project_id)
            # M3：定位前后章节（钩子承接/埋设、AI 味跨章比对）
            previous_id = _chapter_id_before(full_chapter_nodes, chapter["id"])
            next_id = _chapter_id_after(full_chapter_nodes, chapter["id"])
            # M3：生成前注入钩子指令（先承接开放钩子，再埋新钩）
            open_hooks = chapter_hooks_dao.list_open_hooks(job.project_id, limit=5)
            hook_instruction = hook_service.build_hook_instruction(open_hooks, chapter["title"])
            target_words = _chapter_target_word_count(chapter)
            chapter_budget = _chapter_budget_for_job(job, target_words)

            result = {}
            try:
                result = await _generate_chapter_with_expansion(
                    job=job,
                    chapter=chapter,
                    chapter_index=index,
                    total_chapters=len(chapter_nodes),
                    premise=premise,
                    outline_summary=outline_summary,
                    previous_summary=previous_summary,
                    memory_context=memory_context,
                    style_contract=style_contract,
                    characters=characters,
                    world_rules=world_rules,
                    hook_instruction=hook_instruction,
                    budget=chapter_budget,
                )
                # P1-2：章节生成完成后立即检查 cancel，避免继续自检+落库
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.message = "整书创作已取消。"
                    job.finished_at = _utc_iso()
                    job.updated_at = _utc_iso()
                    _save_job(job)
                    break

                # M3：自检闭环（事实核查 → 钩子检测 → AI 味检测），全程降级不阻塞整书
                self_check = await _run_self_check_loop(
                    job=job,
                    chapter=chapter,
                    result=result,
                    memory_context=memory_context,
                    previous_id=previous_id,
                    next_id=next_id,
                    hook_instruction=hook_instruction,
                    world_rules=world_rules,
                    budget=chapter_budget,
                )
                # P1-2：自检完成后再次检查 cancel，避免继续落库
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.message = "整书创作已取消。"
                    job.finished_at = _utc_iso()
                    job.updated_at = _utc_iso()
                    _save_job(job)
                    break
                result["content"] = self_check["content"]
                job.fact_check_count += self_check["fact_check_count"]
                job.rewrite_count += self_check["rewrite_count"]
                job.hook_miss_count += self_check["hook_miss_count"]
                job.quality_debt_count += self_check["quality_debt_count"]
                # 第 3 档：置信度升级次数（容错缺失字段，兼容旧 mock）
                job.escalation_count += int(self_check.get("escalation_count") or 0)
                previous_summary = await _persist_chapter_and_sync_memory(
                    job=job, chapter=chapter, result=result, outline=outline,
                )
                job.preview_content = ""
                job.preview_chapter_id = None
                last_context_summary = _context_reference_summary(
                    characters=characters, memory_context=memory_context,
                    style_contract=style_contract, project_id=job.project_id,
                )
                job.message = last_context_summary
                # M4：生成后统计校验——正文指标 vs 预设允许区间（±20%），偏离计入质量债（P1-1）
                try:
                    deviations = style_profile_service.evaluate_preset_deviation(
                        result.get("content") or "", style_config
                    )
                    job.quality_debt_count += len(deviations)
                except Exception:  # noqa: BLE001
                    pass
                job.completed_chapters += 1
                if quota_tracked:
                    user_quota_service.consume_chapter(quota_user_id)
                if result["used_fallback"]:
                    job.fallback_chapters += 1
                job.last_completed_chapter_id = chapter["id"]
                job.last_completed_chapter_title = chapter["title"]
                job.last_generation_source = result["last_source"]
                job.llm_provider = result["provider"] or job.llm_provider
                job.llm_model = result["model"] or job.llm_model
                job.generated_words = _load_project_word_count(job.project_id)
                job.error = ""
                job.updated_at = _utc_iso()
                _save_job(job)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, style_transition_service.TransitionRevisionConflict):
                    _pause_style_conflict(job, chapter, result, style_contract)
                    break
                last_error = str(exc)
                budget_stop = False
                if isinstance(exc, ChapterBudgetExceeded):
                    last_error = json.dumps(
                        {
                            "code": "chapter_budget_exceeded",
                            "dimension": exc.dimension,
                            "budget": exc.budget.to_dict(),
                        },
                        ensure_ascii=False,
                    )
                    job.failed_chapters += 1
                    job.error = last_error
                    job.status = "failed"
                    job.message = f"《{chapter['title']}》已暂停，等待人工处理"
                    job.finished_at = _utc_iso()
                    job.checkpoint_type = "chapter_budget_exceeded"
                    job.pending_manual_recovery = True
                    job.resume_target_json = json.dumps(
                        {"chapter_id": chapter["id"], "chapter_title": chapter["title"]},
                        ensure_ascii=False,
                    )
                    job.updated_at = _utc_iso()
                    _save_job(job)
                    break
                for attempt in range(1, job.max_retries + 1):
                    if job.cancel_requested:
                        break
                    if not chapter_budget.consume("retry"):
                        last_error = str(ChapterBudgetExceeded("retry", chapter_budget))
                        break
                    job.retry_count += 1
                    job.message = f"正在重试《{chapter['title']}》({attempt}/{job.max_retries})"
                    job.updated_at = _utc_iso()
                    _save_job(job)
                    try:
                        result = await _generate_chapter_with_expansion(
                            job=job,
                            chapter=chapter,
                            chapter_index=index,
                            total_chapters=len(chapter_nodes),
                            premise=premise,
                            outline_summary=outline_summary,
                            previous_summary=previous_summary,
                            memory_context=memory_context,
                            style_contract=style_contract,
                            characters=characters,
                            world_rules=world_rules,
                            hook_instruction=hook_instruction,
                            budget=chapter_budget,
                        )
                        # M3：重试成功同样执行自检闭环（与主路径一致），
                        # 确保重试章节同样经过事实核查 / 钩子检测 / AI 味检测
                        self_check = await _run_self_check_loop(
                            job=job,
                            chapter=chapter,
                            result=result,
                            memory_context=memory_context,
                            previous_id=previous_id,
                            next_id=next_id,
                            hook_instruction=hook_instruction,
                            budget=chapter_budget,
                        )
                        result["content"] = self_check["content"]
                        job.fact_check_count += self_check["fact_check_count"]
                        job.rewrite_count += self_check["rewrite_count"]
                        job.hook_miss_count += self_check["hook_miss_count"]
                        job.quality_debt_count += self_check["quality_debt_count"]
                        # 第 3 档：置信度升级次数（重试路径与主路径一致）
                        job.escalation_count += int(self_check.get("escalation_count") or 0)
                        previous_summary = await _persist_chapter_and_sync_memory(
                            job=job, chapter=chapter, result=result, outline=outline,
                        )
                        job.preview_content = ""
                        job.preview_chapter_id = None
                        last_context_summary = _context_reference_summary(
                            characters=characters, memory_context=memory_context,
                            style_contract=style_contract, project_id=job.project_id,
                        )
                        job.message = last_context_summary
                        # M4：生成后统计校验——正文指标 vs 预设允许区间（P1-1，重试路径一致）
                        try:
                            deviations = style_profile_service.evaluate_preset_deviation(
                                result.get("content") or "", style_config
                            )
                            job.quality_debt_count += len(deviations)
                        except Exception:  # noqa: BLE001
                            pass
                        job.completed_chapters += 1
                        if quota_tracked:
                            user_quota_service.consume_chapter(quota_user_id)
                        if result["used_fallback"]:
                            job.fallback_chapters += 1
                        job.last_completed_chapter_id = chapter["id"]
                        job.last_completed_chapter_title = chapter["title"]
                        job.last_generation_source = result["last_source"]
                        job.llm_provider = result["provider"] or job.llm_provider
                        job.llm_model = result["model"] or job.llm_model
                        job.generated_words = _load_project_word_count(job.project_id)
                        job.error = ""
                        last_error = ""
                        job.updated_at = _utc_iso()
                        _save_job(job)
                        break
                    except Exception as retry_exc:  # noqa: BLE001
                        if isinstance(retry_exc, style_transition_service.TransitionRevisionConflict):
                            _pause_style_conflict(job, chapter, result, style_contract)
                            last_error = ""
                            break
                        if isinstance(retry_exc, ChapterBudgetExceeded):
                            budget_stop = True
                            last_error = json.dumps(
                                {
                                    "code": "chapter_budget_exceeded",
                                    "dimension": retry_exc.dimension,
                                    "budget": retry_exc.budget.to_dict(),
                                },
                                ensure_ascii=False,
                            )
                            break
                        last_error = str(retry_exc)

                if job.checkpoint_type == "style_plan_conflict":
                    break
                if last_error:
                    job.failed_chapters += 1
                    job.error = last_error
                    job.status = "failed"
                    job.message = f"《{chapter['title']}》创作失败：{last_error}"
                    job.finished_at = _utc_iso()
                    job.updated_at = _utc_iso()
                    # M2：失败章写入 checkpoint —— 前端据此渲染"待人工处理"，
                    # resume_job 恢复时跳过该章，从其下一章继续，而非重跑整本书。
                    job.checkpoint_type = (
                        "chapter_budget_exceeded" if budget_stop else "chapter_failed"
                    )
                    job.pending_manual_recovery = True
                    job.resume_target_json = json.dumps(
                        {
                            "chapter_id": chapter["id"],
                            "chapter_title": chapter["title"],
                        },
                        ensure_ascii=False,
                    )
                    _save_job(job)
                    break

        if job.status == "running":
            job.status = "completed"
            if job.failed_chapters == 0:
                job.message = f"完成。{last_context_summary}" if last_context_summary else "完成。"
            else:
                job.message = f"整书创作完成，但仍有 {job.failed_chapters} 个章节失败。"
            job.finished_at = _utc_iso()
            job.updated_at = _utc_iso()
            _save_job(job)
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = str(exc)
        job.message = f"整书创作失败：{exc}"
        job.finished_at = _utc_iso()
        job.updated_at = _utc_iso()
        _save_job(job)
    finally:
        clear_llm_audit_context()
        _jobs.pop(job_id, None)


def _pause_style_conflict(job: NovelGenerationJob, chapter: dict, result: dict, contract: dict) -> None:
    """Durably retain uncommitted output in the job's existing recovery JSON."""
    token = style_transition_service.plan_token(contract)
    job.status = "failed"
    job.failed_chapters += 1
    job.error = str(style_transition_service.TransitionRevisionConflict())
    job.message = job.error
    job.checkpoint_type = "style_plan_conflict"
    job.pending_manual_recovery = True
    job.preview_content = result.get("content") or job.preview_content
    job.preview_chapter_id = chapter["id"]
    job.resume_target_json = json.dumps({"chapter_id": chapter["id"], "chapter_title": chapter["title"],
                                        "candidateContent": job.preview_content, "statusCode": 409, **token}, ensure_ascii=False)
    job.updated_at = job.finished_at = _utc_iso()
    _save_job(job)


async def _run_self_check_loop(
    *,
    job: NovelGenerationJob,
    chapter: dict[str, Any],
    result: dict[str, Any],
    memory_context: dict[str, Any] | None,
    previous_id: str | None,
    next_id: str | None,
    hook_instruction: str,
    world_rules: list[dict[str, Any]] | None = None,
    apply_deterministic_gate: bool = True,
    budget: ChapterGenerationBudget | None = None,
) -> dict[str, Any]:
    """M3 自检闭环编排：事实核查 → 钩子检测 → AI 味检测。

    - 事实核查（fact_check_service.run_fact_check_loop）：LLM 判定客观事实矛盾，
      失败带问题清单重写（max_retries=1，Codex P1-2 压调用），不可调和落盘"写作冲突"；
      全程降级返回原内容。
    - 钩子检测（hook_service）：先承接上一章钩子（更新其状态），再检测本章结尾
      是否留强钩，未留则重写结尾（上限 MAX_SELF_CHECK_HOOK_REWRITES=1 次，每次复检）
      并计数；当前章钩子落库（planted，指向下一章）。
    - AI 味检测（review_service.detect_ai_artifacts）：确定性规则（零 LLM 成本），
      命中累计质量债；超阈值（needs_rewrite=True，存在 high 项）触发开头局部重写
      （rewrite_opening，前 300 字窗口，前文保持），计入 rewrite_count。
    - 文笔重写（rewrite_service.run_style_rewrite_chain）：max_rewrites=1，只对
      最低分 auto 维度执行一次定向重写，其余低分维度记质量债（Codex P1-2）。

    Codex P1-2（单章预算）：`llm_calls` 计数器统计自检阶段 LLM 调用次数，
    超过 MAX_SELF_CHECK_LLM_CALLS 后跳过剩余自检环节（不阻塞落库），
    防止单章调用失控。

    任一环节异常均降级跳过，绝不抛出异常阻塞整书任务。
    返回：
      {
        "content": 最终正文,
        "fact_check_count": 事实核查次数,
        "rewrite_count": 重写次数（事实重写 + 结尾重写）,
        "hook_miss_count": 钩子未命中次数,
        "quality_debt_count": 质量债（AI 味）次数,
        "escalation_count": 置信度升级复查次数（事实核查 + 钩子判定）,
      }
    """
    style_plan_id = result.get("style_plan_id")
    style_plan_revision = result.get("style_plan_revision")

    def validate_style_token() -> None:
        """Reject stale generated output before self-check work consumes an LLM call."""
        if style_plan_id is None and style_plan_revision is None:
            return
        style_transition_service.validate_generation_token(
            job.project_id, style_plan_id, style_plan_revision
        )

    validate_style_token()
    content = result.get("content") or ""
    if job.mode == "single":
        return {
            "content": content,
            "fact_check_count": 0,
            "rewrite_count": 0,
            "hook_miss_count": 0,
            "quality_debt_count": 0,
            "escalation_count": 0,
            "qa_gate": {
                "decision": "skipped",
                "blocking": [],
                "warnings": [],
                "layer1": {"status": "skipped"},
                "layer2": {"status": "skipped", "is_literary_judgment": False},
                "risk": {"level": "controlled_single", "llm_qa_budget": 0, "reasons": ["single_mode"]},
            },
            "llm_qa_calls": 0,
        }
    contract_supplied = "chapter_contract" in result
    contract = result.get("chapter_contract") or ({"goal": "legacy internal call", "chapter_mode": "Conflict"} if not contract_supplied else {})
    prior_chapters = [
        item for item in chapters_dao.list_chapters_global_order(job.project_id)
        if item.get("id") != chapter.get("id")
    ]
    if apply_deterministic_gate:
        foreshadows = foreshadows_dao.list_foreshadows(job.project_id)
        active_foreshadow_names = [
            str(item.get("name") or "")
            for item in foreshadows
            if item.get("status") in {"active", "planted", "progressing"}
        ]
        try:
            actual_story_state_version = int(
                story_state_service.get_story_state(job.project_id).get("version") or 0
            )
        except (ValueError, sqlite3.OperationalError):
            actual_story_state_version = None
        qa_gate = deterministic_qa_service.evaluate_chapter(
            content=content,
            chapter=chapter,
            contract=contract,
            previous_chapters=prior_chapters,
            foreshadows=foreshadows,
            active_foreshadow_names=active_foreshadow_names,
            character_name_rules=(memory_context or {}).get("character_name_rules"),
            known_fact_rules=(memory_context or {}).get("known_fact_rules"),
            timeline_rules=(memory_context or {}).get("timeline_rules"),
            expected_state_version=contract.get("story_state_version"),
            actual_state_version=actual_story_state_version,
            allow_order_gap=bool(
                job.requested_chapter_id
                and chapter.get("id") == job.requested_chapter_id
            ),
        )
        if qa_gate["blocking"]:
            raise RuntimeError(
                "确定性 QA Gate 阻止章节继续："
                + "；".join(item["message"] for item in qa_gate["blocking"])
            )
    else:
        qa_gate = {
            "decision": "continue",
            "blocking": [],
            "warnings": [],
            "layer1": {"status": "not_run"},
            "layer2": {"status": "not_run", "is_literary_judgment": False},
            "risk": {"level": "legacy", "llm_qa_budget": MAX_SELF_CHECK_LLM_CALLS, "reasons": []},
        }
    llm_budget = int(qa_gate["risk"]["llm_qa_budget"]) if contract_supplied else MAX_SELF_CHECK_LLM_CALLS
    if llm_budget <= 0:
        return {
            "content": content, "fact_check_count": 0, "rewrite_count": 0,
            "hook_miss_count": 0, "quality_debt_count": len(qa_gate["warnings"]),
            "escalation_count": 0, "qa_gate": qa_gate, "llm_qa_calls": 0,
        }
    if contract_supplied:
        def allow_contract_review(operation: str, estimated_tokens: int) -> bool:
            validate_style_token()
            if budget is None:
                return True
            allowance = budget.allow_llm_call(operation, estimated_tokens)
            if allowance is None:
                return False
            set_llm_audit_context(
                chapter_id=chapter["id"], operation=allowance.operation,
                budget_dimension=allowance.dimension,
                budget_before=allowance.before, budget_after=allowance.after,
            )
            return True
        reviews: list[dict[str, Any]] = []
        try:
            reviews.append(await review_service.review_logic(content, chapter.get("id") or "", allow_llm_call=allow_contract_review))
            if llm_budget >= 2:
                reviews.append(await review_service.review_language(content, chapter.get("id") or "", allow_llm_call=allow_contract_review))
        except style_transition_service.TransitionRevisionConflict:
            raise
        except Exception:  # noqa: BLE001  审查失败保留正文，由报告暴露失败结果
            pass
        issue_count = sum(len(item.get("issues") or []) for item in reviews)
        return {
            "content": content,
            "fact_check_count": 0,
            "rewrite_count": 0,
            "hook_miss_count": 0,
            "quality_debt_count": len(qa_gate["warnings"]) + issue_count,
            "escalation_count": 0,
            "qa_gate": qa_gate,
            "llm_qa_calls": len(reviews),
            "llm_reviews": reviews,
        }
    fact_check_count = 0
    rewrite_count = 0
    hook_miss_count = 0
    quality_debt_count = 0
    escalation_count = 0
    # 文笔质量固化：钩子检测最终结果（供第 ④ 步 D9 评分）；AI 味检测用的上一章文本
    hook_passed: bool | None = None
    previous_text = ""
    # Codex P1-2：单章自检 LLM 调用预算（正文生成不计入，只约束自检阶段）
    llm_calls = 0
    llm_call_limit = min(
        MAX_SELF_CHECK_LLM_CALLS,
        llm_budget,
        budget.max_qa_calls - budget.qa_calls if budget else MAX_SELF_CHECK_LLM_CALLS,
    )
    # Codex P2 复测 P1：事实冲突质量门槛——事实核查未通过（不可调和冲突）时，
    # 阻止章节静默通过：计入质量债 + 强制触发一次定向重写。
    fact_conflict_pending = False
    fact_conflict_issues: list[dict[str, Any]] = []

    def allow_rewrite() -> bool:
        return budget is None or budget.consume("rewrite")

    def allow_llm_call(operation: str, estimated_tokens: int) -> bool:
        """Consume the shared chapter budget before any delegated LLM call."""
        validate_style_token()
        if budget is None:
            return True
        allowance = budget.allow_llm_call(operation, estimated_tokens)
        if allowance is None:
            return False
        set_llm_audit_context(
            chapter_id=chapter["id"], operation=allowance.operation,
            budget_dimension=allowance.dimension,
            budget_before=allowance.before, budget_after=allowance.after,
        )
        return True

    # ① 事实核查（LLM 失败降级返回原内容；max_retries=1 压调用）
    try:
        fc = await fact_check_service.run_fact_check_loop(
            project_id=job.project_id,
            chapter_id=chapter["id"],
            content=content,
            memory_context=_with_world_rules(memory_context, world_rules),
            max_retries=0 if llm_call_limit == 1 else 1,
            allow_llm_call=allow_llm_call,
        )
        content = fc.get("content") or content
        stats = fc.get("stats") or {}
        fact_check_count += int(stats.get("fact_check_count") or 0)
        rewrite_count += int(stats.get("rewrite_count") or 0)
        escalation_count += int(stats.get("escalation_count") or 0)
        # 估算事实核查 LLM 调用：核查次数 + 重写次数 + 根因分析（若有冲突）+ 最终复检
        llm_calls += int(stats.get("fact_check_count") or 0)
        llm_calls += int(stats.get("rewrite_count") or 0)
        if fc.get("conflict"):
            llm_calls += 1  # analyze_conflict
            llm_calls += 1  # 可调和时的最终核查
        # Codex P2 复测 P1：事实核查未通过（passed=False，存在不可调和冲突）→
        # 设为质量门槛：计入质量债 + 标记待重写（阻止静默通过）。
        if fc.get("passed") is False:
            fact_conflict_pending = True
            fact_conflict_issues = fc.get("issues") or []
            conflict = fc.get("conflict") or {}
            conflict_detail = conflict.get("summary") or conflict.get("root_cause") or "事实核查未通过"
            quality_debt_count += 1
            _logger.warning(
                "章节 %s 事实核查未通过（不可调和冲突）：%s——计入质量债并触发定向重写。",
                chapter["id"], conflict_detail,
            )
    except style_transition_service.TransitionRevisionConflict:
        raise
    except Exception:  # noqa: BLE001  全程降级，绝不阻塞整书任务
        pass

    # ② 钩子检测（LLM 失败降级 has_hook=True，跳过重写不阻塞）
    try:
        # 先承接上一章钩子：更新其状态（planted → carried/resolved）
        if previous_id:
            await hook_service.update_previous_hooks(
                job.project_id, previous_id, content, allow_llm_call
            )
        # 再检测本章结尾是否留有强钩；未留则重写结尾（上限 MAX_SELF_CHECK_HOOK_REWRITES
        # 次，每次重写后复检，仍无钩则停止，避免死循环）；只复检不重复计数
        check = await hook_service.check_hook(content, allow_llm_call)
        llm_calls += 1
        rewrite_attempts = 0
        while (
            not check.get("has_hook")
            and rewrite_attempts < MAX_SELF_CHECK_HOOK_REWRITES
            and llm_calls < llm_call_limit
        ):
            if rewrite_attempts == 0:
                hook_miss_count += 1
            escalation_count += int(check.get("escalation_count") or 0)
            if not allow_rewrite():
                break
            rewritten = await hook_service.rewrite_ending(content, allow_llm_call)
            llm_calls += 1
            if not rewritten or rewritten == content:
                break  # 重写降级/无变化，停止
            content = rewritten
            rewrite_count += 1
            rewrite_attempts += 1
            check = await hook_service.check_hook(content, allow_llm_call)
            llm_calls += 1
        hook_passed = bool(check.get("has_hook"))
        # 当前章钩子落库（planted，指向下一章承接）
        hook_service.record_hook(
            job.project_id, chapter["id"], check, target_chapter=next_id or ""
        )
        escalation_count += int(check.get("escalation_count") or 0)
    except style_transition_service.TransitionRevisionConflict:
        raise
    except Exception:  # noqa: BLE001  全程降级，绝不阻塞整书任务
        pass

    # ③ AI 味检测（确定性检查，零 LLM 成本；超阈值触发局部重写）
    try:
        if previous_id:
            previous_text = _load_existing_chapter_text(job.project_id, previous_id)
        ai = review_service.detect_ai_artifacts(content, previous_text)
        debt_items = ai.get("debt_items") or []
        quality_debt_count += len(debt_items)
        # Codex P1-3：跨章整段重复检测——当前章与之前所有已落库章节比对，
        # 命中 ≥AI_CROSS_NGRAM_HIGH_LEN 的整段复制 → high（阻止章节通过，触发重写）；
        # 否则 medium（质量债）。零 LLM 成本。
        try:
            prior_texts = _load_prior_chapter_texts(job.project_id, chapter["id"])
            cross_repeats = review_service._cross_chapter_ngram_repeat(content, prior_texts)
            for repeat in cross_repeats:
                phrase = repeat["phrase"]
                is_long = len(phrase) >= review_service.AI_CROSS_NGRAM_HIGH_LEN
                severity = "high" if is_long else "medium"
                debt_items.append(
                    {
                        "type": "cross_chapter_repeat",
                        "severity": severity,
                        "detail": (
                            f"跨章整段重复 {len(phrase)} 字："
                            f"{phrase[:30]}{'…' if len(phrase) > 30 else ''}"
                        ),
                    }
                )
                ai["issues"].append(
                    {
                        "severity": severity,
                        "type": "跨章整段重复",
                        "location": "跨章",
                        "original": phrase,
                        "suggestion": "改写这段内容，避免与前章整段雷同；推进新情节而非复读前文。",
                        "reason": (
                            f"本章与之前章节出现 {len(phrase)} 字完全相同的连续文本，"
                            "疑似模型复读前文，AI 味明显。"
                        ),
                    }
                )
            if cross_repeats:
                quality_debt_count += len(cross_repeats)
                # 存在 high 跨章重复 → 强制 needs_rewrite（阻止章节通过）
                if any(len(r["phrase"]) >= review_service.AI_CROSS_NGRAM_HIGH_LEN for r in cross_repeats):
                    ai["needs_rewrite"] = True
        except Exception:  # noqa: BLE001  跨章检测失败降级，不阻塞
            pass
        # Codex P2 复测 P1：事实冲突质量门槛——事实核查未通过时强制触发重写，
        # 尝试消除冲突（阻止章节静默通过）。
        if fact_conflict_pending:
            ai["needs_rewrite"] = True
            ai["issues"].append(
                {
                    "severity": "high",
                    "type": "事实冲突",
                    "location": "全文",
                    "original": "",
                    "suggestion": "修正与既有事实账本矛盾的内容，遵守已确立的角色身份/时间线/道具数量/方向等设定。",
                    "reason": "事实核查发现不可调和冲突，需定向重写消除矛盾。",
                }
            )
        # 超阈值（存在 high 项）→ 局部重写：
        #  - 同章原句重复（≥20 字完整复制）→ 按片段实际位置重写（Codex C3 闭环），
        #    不再只重写前 300 字；
        #  - 其他 AI 味（开头重复/句式/连接词）→ 沿用 rewrite_opening（前 300 字）。
        #  - 事实冲突（fact_conflict_pending）→ 用 fact_check_service.rewrite_content
        #    带问题清单 + 事实账本上下文定向重写（Codex P2 复测 P1：AI 味重写不处理
        #    事实冲突，必须用事实核查专用重写），重写后复检，通过才接受。
        # LLM 失败 / 无变化时降级返回原文，此处不重复计数。
        if ai.get("needs_rewrite") and llm_calls < llm_call_limit:
            original_content = content  # Codex D2：改写前保存原文，复检失败回滚
            if fact_conflict_pending and fact_conflict_issues:
                # 事实冲突专用重写：带问题清单 + 事实账本上下文，消除矛盾
                rewritten = await fact_check_service.rewrite_content(
                    content, fact_conflict_issues, memory_context, allow_llm_call=allow_llm_call
                )
                llm_calls += 1
                if rewritten and rewritten != content:
                    # 复检：事实核查通过才接受；否则回滚原文（保留冲突落盘）
                    try:
                        recheck = await fact_check_service.check_chapter(
                            job.project_id, chapter["id"], rewritten, memory_context, allow_llm_call
                        )
                        llm_calls += 1
                        if recheck.get("result") != "FAIL":
                            content = rewritten
                            rewrite_count += 1
                            fact_conflict_pending = False  # 冲突已消除
                        else:
                            content = original_content
                    except style_transition_service.TransitionRevisionConflict:
                        raise
                    except Exception:  # noqa: BLE001  复检失败降级 → 保守回滚原文
                        content = original_content
                else:
                    content = original_content
            else:
                validate_style_token()
                rewritten = await review_service.rewrite_ngram_repeat(content, ai)
                llm_calls += 1
                if rewritten and rewritten != content:
                    # D2 复检：仅在"无高严重度重复 且 评分不下降"时接受重写；
                    # 否则恢复原文，且不增加 rewrite_count（防损坏正文落库）。
                    accepted = False
                    try:
                        recheck = review_service.detect_ai_artifacts(rewritten, previous_text)
                        accepted = review_service._should_accept_ngram_rewrite(ai, recheck)
                    except Exception:  # noqa: BLE001  复检失败降级 → 保守回滚原文
                        accepted = False
                    if accepted:
                        content = rewritten
                        rewrite_count += 1
                    else:
                        content = original_content  # 复检失败/损坏 → 回滚原文
                else:
                    validate_style_token()
                    rewritten = await review_service.rewrite_opening(content, ai)
                    if rewritten and rewritten != content:
                        content = rewritten
                        rewrite_count += 1
    except style_transition_service.TransitionRevisionConflict:
        raise
    except Exception:  # noqa: BLE001  全程降级，绝不阻塞整书任务
        pass

    # ④ 文笔局部重写（文笔质量固化 · Codex 方案执行顺序第 8 步）
    # 评分量表驱动：低分维度（≤2）触发对应局部重写（开头/对话/行动/情绪/结尾），
    # 每次重写落审计表（chapter_rewrites：原文/原因/结果/评分前后），
    # 自动回退保证不降低事实一致性与综合评分；「必须人工」维度只记质量债（debt），
    # 不自动重写（量表第八节：能确定性的交给自动化，能语义判断的交给人工）。
    # Codex P1-2：max_rewrites=1，只对最低分 auto 维度执行一次定向重写。
    # 全程降级：任何 LLM 失败返回原内容，绝不阻塞整书任务。
    try:
        if llm_calls < llm_call_limit:
            style_result = await rewrite_service.run_style_rewrite_chain(
                project_id=job.project_id,
                chapter_id=chapter["id"],
                content=content,
                previous_text=previous_text,
                memory_context=memory_context,
                is_first_chapter=previous_id is None,
                has_hook=hook_passed,
                max_rewrites=1,
                allow_llm_call=allow_llm_call,
            )
            content = style_result.get("content") or content
            rewrite_count += int(style_result.get("rewrite_count") or 0)
            quality_debt_count += len(style_result.get("debt_items") or [])
            llm_calls += int(style_result.get("rewrite_count") or 0)
    except style_transition_service.TransitionRevisionConflict:
        raise
    except Exception:  # noqa: BLE001  全程降级，绝不阻塞整书任务
        pass

    return {
        "content": content,
        "fact_check_count": fact_check_count,
        "rewrite_count": rewrite_count,
        "hook_miss_count": hook_miss_count,
        "quality_debt_count": quality_debt_count,
        "escalation_count": escalation_count,
        "qa_gate": qa_gate,
        "llm_qa_calls": min(llm_calls, llm_call_limit),
    }


async def _generate_chapter_with_expansion(
    *,
    job: NovelGenerationJob,
    chapter: dict[str, Any],
    chapter_index: int,
    total_chapters: int,
    premise: str,
    outline_summary: str,
    previous_summary: str = "",
    memory_context: dict[str, Any] | None = None,
    style_contract: dict[str, Any] | None = None,
    characters: list[dict[str, Any]],
    world_rules: list[dict[str, Any]],
    hook_instruction: str = "",
    budget: ChapterGenerationBudget | None = None,
) -> dict[str, Any]:
    budget = budget or ChapterGenerationBudget()

    def reserve_generation_call(estimated_tokens: int) -> None:
        allowance = budget.allow_llm_call("generation", estimated_tokens=estimated_tokens)
        if allowance is None:
            raise ChapterBudgetExceeded("generation", budget)
        set_llm_audit_context(
            project_id=job.project_id, job_id=job.id, chapter_id=chapter["id"],
            operation=allowance.operation, budget_dimension=allowance.dimension,
            budget_before=allowance.before, budget_after=allowance.after,
        )
    # M4：结构化风格契约 → 编译成注入提示块（预设五维 + 画像规则 + 过渡档位微调）
    style_tone = style_profile_service.contract_to_prompt_block(style_contract)
    compact_characters = _compact_characters(characters)
    compact_world_rules = _compact_world_rules(world_rules)
    existing_text = _load_existing_chapter_text(job.project_id, chapter["id"])
    target_words = _chapter_target_word_count(chapter)
    try:
        chapter_contract = chapter_contract_service.get_or_compile_chapter_contract(
            job.project_id, chapter["id"]
        )
    except ValueError:
        # 旧测试库/旧本地库可能尚无合同表；仅该情况允许沿用旧生成路径。
        # 当前 schema 已有合同表时，合同编译失败必须继续抛出，由任务进入失败恢复。
        with get_conn() as conn:
            has_contract_schema = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chapter_contracts'"
            ).fetchone()
        if has_contract_schema:
            raise
        chapter_contract = None
    except sqlite3.OperationalError:
        # 老库/纯内存测试库尚未执行 P0-B 迁移时，保留原生成路径。
        chapter_contract = None

    scoped_chapter = chapter
    scoped_outline_summary = outline_summary
    outline_scope = ((chapter_contract or {}).get("arc_context") or {}).get("outline_scope") or {}
    if outline_scope.get("arc_title"):
        compact_characters = [{**character, "arc": None, "deepDesire": None} for character in compact_characters]
        arc_summary = str(outline_scope.get("arc_summary") or "").strip()
        # The book premise may reveal the ending just as the global outline
        # does. Scope every duplicated prompt field, not only outlineSummary.
        premise = arc_summary
        hook_instruction = ""
        if not outline_scope.get("prior_chapter_ids"):
            previous_summary = ""
        scoped_outline_summary = (
            f"当前卷：{outline_scope.get('volume_title') or '当前卷'}；"
            f"当前剧情弧：{outline_scope.get('arc_title')}；"
            f"本弧范围：{arc_summary or '只推进当前剧情弧。'}；"
            "其他剧情弧尚未发生，不得提前写入。"
        )
        scoped_chapter = {
            **chapter,
            "summary": arc_summary or str((chapter_contract or {}).get("goal") or ""),
            "conflict": str((chapter_contract or {}).get("conflict") or ""),
            "revelation": str((chapter_contract or {}).get("state_change") or ""),
        }

    # M3：钩子指令（先承接上一章开放钩子，再埋新钩）注入本章写作方向
    base_direction = _build_direction(scoped_chapter)
    if hook_instruction:
        base_direction = f"{base_direction}\n\n{hook_instruction}"
    if chapter_contract:
        base_direction = (
            f"{base_direction}\n\n"
            f"{chapter_contract_service.render_contract(chapter_contract)}"
        )
        voice_text = character_voice_service.render_voice_profiles(
            {"id": job.project_id, "characters": characters},
            chapter_contract.get("characters") or [],
            **({"include_plot": False} if outline_scope else {}),
        )
        if voice_text:
            base_direction += "\n\n" + voice_text
        experience_text = "" if outline_scope else writing_experience_service.render_accepted(job.project_id)
        if experience_text:
            base_direction += "\n\n" + experience_text
    try:
        story_truth = story_state_service.get_story_state(job.project_id)
    except (ValueError, sqlite3.OperationalError):
        story_truth = {}
    if outline_scope:
        # A project-wide state is the latest state, not a historical snapshot
        # at the chapter being regenerated. The current contract and bounded
        # prior-chapter context are the available temporal sources here.
        story_truth = {}
    scene_guidance = scene_guidance_service.build_scene_guidance(
        characters=characters,
        world_rules=world_rules,
        chapter_contract=chapter_contract,
        story_state=story_truth,
    )
    if scene_guidance:
        base_direction += "\n\n" + scene_guidance

    # 阶段E：写前上下文（contextBefore）哈希——提交记录的可追溯锚点
    context_before = _build_context_before(
        project_id=job.project_id,
        chapter_id=chapter["id"],
        premise=premise,
        chapter=scoped_chapter,
        chapter_index=chapter_index,
        total_chapters=total_chapters,
        outline_summary=scoped_outline_summary,
        previous_summary=previous_summary,
        existing_text=existing_text,
        memory_context=memory_context,
        chapter_contract=chapter_contract,
    )
    context_hash = chapter_commits_dao.sha256_hex(context_before)

    req = ContinueWritingRequest(
        projectId=job.project_id,
        chapterId=chapter["id"],
        cursorPosition=0,
        contextBefore=context_before,
        direction=base_direction,
        wordCount=target_words,
        chapterTitle=chapter["title"],
        premise=premise,
        outlineSummary=scoped_outline_summary,
        chapterSummary=scoped_chapter.get("summary", ""),
        styleTone=style_tone,
        **style_transition_service.plan_token(style_contract or {}),
        characters=compact_characters,
        worldRules=compact_world_rules,
        maxAttempts=1,
    )
    # The same compiled snapshot drives the prompt and the eventual commit.
    # Private server-only state cannot be supplied by a client JSON payload.
    req._style_contract = style_contract
    style_transition_service.validate_generation_token(job.project_id, req.stylePlanId, req.stylePlanRevision)

    reserve_generation_call(target_words)
    job.preview_chapter_id = chapter["id"]
    job.preview_content = ""
    _publish_job_update(job)

    def on_chunk(chunk: str) -> None:
        job.preview_content += chunk
        job.updated_at = _utc_iso()
        _publish_job_update(job)

    # 保持旧测试/调用方对 generate_continuation_result 的 monkeypatch 兼容；
    # 正常运行时使用流式实现，测试替换旧入口时仍走其 deterministic mock。
    if getattr(generate_continuation_result, "__module__", "") != "app.services.ai_service":
        generation = await generate_continuation_result(req)
    else:
        generation = await generate_continuation_stream_result(req, on_chunk=on_chunk)
    # P0-2：3 次 LLM 失败 → source="failed"，正文为空、不落库；抛错由上层
    # 标记 chapter_failed / pending_manual_recovery，不增 completed/字数。
    if generation.get("source") == "failed":
        raise RuntimeError(
            f"模型生成失败（已重试 3 次）：{generation.get('error') or '未知错误'}"
        )
    if generation.get("generation_incomplete"):
        raise GenerationIncompleteError("模型输出在达到长度上限前结束，未提交章节。")
    content = _normalize_generated_segment(generation["text"], chapter["title"])
    if not content:
        raise RuntimeError("模型返回了空章节内容。")

    used_fallback = False  # P0-2：fallback 不再作为正文落库
    provider = generation.get("provider", "")
    model = generation.get("model", "")
    last_source = generation.get("source", "")
    minimum_words = (
        budget.minimum_words
        if job.mode == "single" and budget.minimum_words > 0
        else _minimum_acceptable_words(target_words)
    )

    expansion_passes = () if job.mode == "single" else range(1, MAX_EXPANSION_PASSES + 1)
    for expansion_pass in expansion_passes:
        actual_words = _count_words(content)
        if actual_words >= minimum_words:
            break
        if job.cancel_requested:
            break

        missing_words = max(target_words - actual_words, MIN_EXPANSION_WORDS)
        requested_words = min(
            max(missing_words + EXPANSION_WORD_BUFFER, MIN_EXPANSION_WORDS),
            MAX_EXPANSION_WORDS,
        )
        job.message = (
            f"正在补写《{chapter['title']}》细节"
            f" ({expansion_pass}/{MAX_EXPANSION_PASSES}, {actual_words}/{target_words} 字)"
        )
        job.updated_at = _utc_iso()
        _save_job(job)

        expansion_contract = chapter_contract_service.render_contract(chapter_contract) if chapter_contract else ""
        expansion_req = ContinueWritingRequest(
            projectId=job.project_id,
            chapterId=chapter["id"],
            cursorPosition=actual_words,
            contextBefore=content[-4000:],
            direction=(
                _build_expansion_direction(
                    chapter=scoped_chapter,
                    actual_words=actual_words,
                    target_words=target_words,
                    expansion_pass=expansion_pass,
                    chapter_contract=expansion_contract,
                )
                + ("\n\n" + scene_guidance if scene_guidance else "")
            ),
            wordCount=requested_words,
            chapterTitle=chapter["title"],
            premise=premise,
            outlineSummary=scoped_outline_summary,
            chapterSummary=scoped_chapter.get("summary", ""),
            styleTone=style_tone,
            **style_transition_service.plan_token(style_contract or {}),
            characters=compact_characters,
            worldRules=compact_world_rules,
            maxAttempts=1,
        )
        expansion_req._style_contract = style_contract
        style_transition_service.validate_generation_token(job.project_id, expansion_req.stylePlanId, expansion_req.stylePlanRevision)
        reserve_generation_call(requested_words)
        if getattr(generate_continuation_result, "__module__", "") != "app.services.ai_service":
            expansion = await generate_continuation_result(expansion_req)
        else:
            expansion = await generate_continuation_stream_result(expansion_req, on_chunk=on_chunk)
        # P0-2：补写失败同样不落库，抛错走重试/失败路径
        if expansion.get("source") == "failed":
            raise RuntimeError(
                f"{chapter['title']} 补写失败（已重试 3 次）：{expansion.get('error') or '未知错误'}"
            )
        if expansion.get("generation_incomplete"):
            raise GenerationIncompleteError("模型补写在达到长度上限前结束，未提交章节。")
        addition = _normalize_generated_segment(expansion["text"], chapter["title"])
        if not addition:
            raise RuntimeError(f"{chapter['title']} 在补写轮次 {expansion_pass} 返回空内容。")

        repeated = review_service._cross_chapter_ngram_repeat(addition, [content])
        if any(int(item.get("length") or len(item.get("phrase") or "")) >= review_service.AI_CROSS_NGRAM_HIGH_LEN for item in repeated):
            raise ChapterBudgetExceeded("repeated-expansion", budget)

        content = _merge_chapter_content(content, addition)
        used_fallback = False  # P0-2：fallback 不落库
        provider = expansion.get("provider", "") or provider
        model = expansion.get("model", "") or model
        last_source = expansion.get("source", "") or last_source

    final_words = _count_words(content)
    if final_words < minimum_words and not job.cancel_requested:
        if job.mode == "single":
            job.preview_chapter_id = chapter["id"]
            job.preview_content = content
            raise GenerationIncompleteError(
                f"{chapter['title']} 单次生成仅达到 {final_words}/{minimum_words} 字硬下限，"
                "未提交章节且未自动补写。"
            )
        raise RuntimeError(
            f"{chapter['title']} 在补写后仅达到 {final_words}/{target_words} 字。"
        )

    response = {
        "content": content,
        "used_fallback": used_fallback,
        "provider": provider,
        "model": model,
        "last_source": last_source,
        "audit_id": generation.get("audit_id") or "",
        "context_hash": context_hash,
    }
    transition_block = (style_contract or {}).get("transition") or {}
    response["style_plan_id"] = transition_block.get("planId") or ""
    response["style_plan_revision"] = transition_block.get("revision") or 0
    if chapter_contract is not None:
        response["chapter_contract"] = chapter_contract
    return response


def _load_project(project_id: str) -> dict[str, Any]:
    project = projects_dao.get_project(project_id)
    if not project:
        raise RuntimeError("项目不存在。")
    return project


def _chapter_nodes_from_outline(outline: dict[str, Any]) -> list[dict[str, Any]]:
    """返回全局有序的章节节点列表。

    BUG-B（记忆升级 v1）：大纲节点 `order` 是**卷内序号**（跨卷重复，如多卷项目
    每卷都有 order=1/2/3…），原 `sorted(key=(order, title))` 会把各卷的"第一章"
    排在一起导致跨卷顺序错乱（_load_previous_summary / _build_memory_context 的
    前一章定位与剧透过滤全部依赖此顺序）。

    修复：以「卷节点在数组中的出现顺序」作为全局卷序，章节按
    `(卷全局序, 卷内 order)` 复合排序——多卷项目跨卷顺序正确，单卷/无卷项目
    退化为纯 order 排序（与原行为一致）。
    """
    nodes = outline.get("nodes", []) if isinstance(outline, dict) else []
    volume_order_index: dict[str, int] = {}
    volume_idx = 0
    for node in nodes:
        if node.get("type") == "volume":
            vid = node.get("id")
            if vid and vid not in volume_order_index:
                volume_order_index[vid] = volume_idx
                volume_idx += 1

    arcs = {node.get("id"): node for node in nodes if node.get("type") == "arc"}
    # Some historical outlines nest chapters directly under arcs. Normalize
    # the request view without modifying the author's stored outline.
    chapters = []
    for node in nodes:
        if node.get("type") != "chapter":
            continue
        parent_arc = arcs.get(node.get("parentId"))
        chapters.append({**node, "parentId": parent_arc.get("parentId"), "arcId": node.get("arcId") or parent_arc.get("id")}
                        if parent_arc else node)

    def _sort_key(node: dict[str, Any]) -> tuple[int, int]:
        parent = node.get("parentId") or ""
        vol_idx = volume_order_index.get(parent, volume_idx)  # 无卷归属的排最后
        return (vol_idx, int(node.get("order") or 0))

    return sorted(chapters, key=_sort_key)


def _resolve_start_chapter_id(
    *,
    project_id: str,
    chapter_nodes: list[dict[str, Any]],
    mode: str,
    requested_chapter_id: str | None,
) -> str | None:
    if requested_chapter_id:
        return requested_chapter_id

    if mode == "first-unfinished":
        resume_job = _find_resume_job(project_id)
        if resume_job:
            if (
                resume_job.current_chapter_id
                and resume_job.current_chapter_id != resume_job.last_completed_chapter_id
            ):
                return resume_job.current_chapter_id
            if resume_job.last_completed_chapter_id:
                next_id = _chapter_id_after(chapter_nodes, resume_job.last_completed_chapter_id)
                if next_id:
                    return next_id

        first_unfinished = _find_first_unfinished_chapter_id(project_id, chapter_nodes)
        return first_unfinished

    return requested_chapter_id


def _select_chapter_nodes(
    project_id: str,
    chapter_nodes: list[dict[str, Any]],
    mode: str,
    current_chapter_id: str | None,
) -> list[dict[str, Any]]:
    if mode in {"current-to-end", "first-unfinished", "single"} and current_chapter_id:
        for index, chapter in enumerate(chapter_nodes):
            if chapter["id"] == current_chapter_id:
                return chapter_nodes[index:index + 1] if mode == "single" else chapter_nodes[index:]
        return chapter_nodes

    if mode == "first-unfinished":
        first_unfinished = _find_first_unfinished_chapter_id(project_id, chapter_nodes)
        if not first_unfinished:
            return []
        for index, chapter in enumerate(chapter_nodes):
            if chapter["id"] == first_unfinished:
                return chapter_nodes[index:]
        return []

    return chapter_nodes


def _materialize_outline_chapters(project_id: str, chapter_nodes: list[dict[str, Any]]) -> None:
    """Create durable empty chapter rows for selected outline chapters.

    Outline nodes are the navigation and generation source of truth, while
    Chapter Contract and commit services require a matching ``chapters`` row.
    Materialization is idempotent and deliberately preserves any existing
    chapter content and metadata.
    """
    if not chapter_nodes:
        return
    today = utc_today()
    with get_conn() as conn:
        existing_ids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM chapters WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        }
        for chapter in chapter_nodes:
            if chapter.get("type") != "chapter":
                continue
            chapter_id = str(chapter.get("id") or "").strip()
            if not chapter_id or chapter_id in existing_ids:
                continue
            chapters_dao._insert_chapter_on_conn(conn, {
                "id": chapter_id,
                "project_id": project_id,
                "volume_id": str(chapter.get("parentId") or ""),
                "title": repair_text(str(chapter.get("title") or "Untitled Chapter")),
                "chapter_order": int(chapter.get("order") or 1),
                "content": "",
                "word_count": 0,
                "status": "draft",
                "summary": repair_text(str(chapter.get("summary") or "")),
                "notes": "",
                "created_at": today,
                "updated_at": today,
            })


def _find_resume_job(project_id: str) -> NovelGenerationJob | None:
    jobs = list_project_jobs(project_id, limit=10)
    for job in jobs:
        if job.status == "completed":
            continue
        if job.total_chapters > 0 and job.completed_chapters < job.total_chapters:
            return job
    return None


def _find_first_unfinished_chapter_id(
    project_id: str,
    chapter_nodes: list[dict[str, Any]],
) -> str | None:
    for chapter in chapter_nodes:
        existing_text = _load_existing_chapter_text(project_id, chapter["id"])
        threshold = max(int(_chapter_target_word_count(chapter) * 0.35), 300)
        if _count_words(existing_text) < threshold:
            return chapter["id"]
    return None


def _chapter_id_after(chapter_nodes: list[dict[str, Any]], chapter_id: str) -> str | None:
    for index, chapter in enumerate(chapter_nodes):
        if chapter["id"] == chapter_id and index + 1 < len(chapter_nodes):
            return chapter_nodes[index + 1]["id"]
    return None


def _chapter_id_before(chapter_nodes: list[dict[str, Any]], chapter_id: str) -> str | None:
    """返回指定章节的前一章 id；无前一章返回 None（M3 钩子承接/AI 味比对用）。"""
    for index, chapter in enumerate(chapter_nodes):
        if chapter["id"] == chapter_id and index > 0:
            return chapter_nodes[index - 1]["id"]
    return None


def _failed_chapter_has_successor(job: NovelGenerationJob) -> bool:
    """失败章（resume_target_json.chapter_id）之后是否仍有可继续的章节。

    无恢复目标 / 目标解析失败 / 项目 outline 缺失时保守返回 True（视为可恢复），
    交由 resume_job 的末章守卫兜底 400，避免列表误过滤。
    """
    if not job.pending_manual_recovery or not job.resume_target_json:
        return True
    try:
        target = json.loads(job.resume_target_json)
    except (TypeError, ValueError):
        return True
    failed_id = (target.get("chapter_id") or "").strip()
    if not failed_id:
        return True
    try:
        project = _load_project(job.project_id)
    except RuntimeError:
        return True
    outline = project.get("outline") or {}
    chapter_nodes = _chapter_nodes_from_outline(outline)
    return _chapter_id_after(chapter_nodes, failed_id) is not None


def _is_job_recoverable(job: NovelGenerationJob) -> bool:
    """任务是否可恢复（综合判断，含末章失败场景）。

    P0/P1（Codex 复测）：失败章任务**默认重试失败章本身**——末章失败也能重试，
    不再要求失败章之后有后继章节（此前末章失败被判定不可恢复，导致整书缺末章）。
    只要未全部完成且处于可中断状态即视为可恢复；具体恢复起点由 resume_job 决定。
    """
    if job.checkpoint_type == "style_plan_conflict":
        return False
    if job.status not in {"interrupted", "failed", "cancelled"}:
        return False
    if job.total_chapters > 0 and job.completed_chapters >= job.total_chapters:
        return False
    # A previous attempt can fail before a chapter row exists, then a later
    # single-chapter run can successfully save that same target.  Do not keep
    # offering the old failed job as a paid retry once the target has usable
    # prose; the persisted chapter is the source of truth.
    if job.status == "failed" and job.pending_manual_recovery and job.resume_target_json:
        try:
            target = json.loads(job.resume_target_json)
            failed_id = (target.get("chapter_id") or "").strip()
            project = _load_project(job.project_id)
            nodes = _chapter_nodes_from_outline(project.get("outline") or {})
            node = next((item for item in nodes if item["id"] == failed_id), None)
            if node:
                threshold = max(int(_chapter_target_word_count(node) * 0.35), 300)
                if _count_words(_load_existing_chapter_text(job.project_id, failed_id)) >= threshold:
                    return False
        except (RuntimeError, TypeError, ValueError):
            # A malformed historical job remains visible for manual recovery.
            pass
    return True


def _load_previous_summary(
    project_id: str,
    chapter_nodes: list[dict[str, Any]],
    requested_chapter_id: str | None,
) -> str:
    """加载当前章的"真正前一章"摘要（BUG-B 修复后全局顺序定位）。

    BUG-B（记忆升级 v1）：大纲节点 `order` 是卷内序号（跨卷重复），原实现按
    `chapter_nodes[index-1]` 直接取前一个节点，但因 `_chapter_nodes_from_outline`
    排序错乱，跨卷场景会取到错误章节。修复后 `chapter_nodes` 已是全局有序
    （卷全局序 + 卷内 order），再经 `chapters_dao.get_previous_chapter` 回查
    chapters 表（须已落库），取前一章正文做截断摘要。
    """
    if not requested_chapter_id:
        return ""

    chapter_ids_in_order = [node["id"] for node in chapter_nodes]
    previous = chapters_dao.get_previous_chapter(
        project_id, requested_chapter_id, chapter_ids_in_order=chapter_ids_in_order
    )
    if not previous:
        return ""

    existing = _load_existing_chapter_text(project_id, previous["id"])
    return _summarize(existing)


def _compact_characters(characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for character in characters[:10]:
        basic = character.get("basicInfo") or {}
        compact.append(
            {
                "name": character.get("name", ""),
                "role": character.get("role"),
                "identity": character.get("identity") or basic.get("identity"),
                "gender": character.get("gender") or basic.get("gender"),
                "surfaceMotivation": character.get("surfaceMotivation"),
                "deepDesire": character.get("deepDesire"),
                "arc": character.get("arc"),
                "speechStyle": character.get("speechStyle"),
                "habits": character.get("habits"),
                "values": character.get("values"),
            }
        )
    return compact


def _with_world_rules(
    memory_context: dict[str, Any] | None,
    world_rules: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """把世界规则挂进记忆上下文，供事实核查的「第 5 类：设定矛盾」使用。

    此前核查上下文只有「卷摘要 + 近章摘要 + 事实账本」，`world_rules` 从未传进去，
    于是“世界观规则矛盾”这一类在自动创作里等于不可判。
    """
    if not world_rules:
        return memory_context
    return {**(memory_context or {}), "world_rules": list(world_rules)}


def _compact_world_rules(world_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只做字段归一化，**不在此截断**。

    旧实现取前 12 条，导致第 13 条以后的规则永远不会进入 prompt（静默丢失）。
    真正的篇幅控制在渲染侧按字数预算做，并能把省略条数告诉模型。
    """
    return [
        {
            "category": rule.get("category"),
            "name": rule.get("name", ""),
            "description": rule.get("description", ""),
        }
        for rule in world_rules
        if isinstance(rule, dict)
    ]


def _build_context_before(
    *,
    project_id: str = "",
    chapter_id: str = "",
    premise: str,
    chapter: dict[str, Any],
    chapter_index: int,
    total_chapters: int,
    outline_summary: str,
    previous_summary: str = "",
    existing_text: str = "",
    memory_context: dict[str, Any] | None = None,
    chapter_contract: dict[str, Any] | None = None,
) -> str:
    outline_scope = ((chapter_contract or {}).get("arc_context") or {}).get("outline_scope") or {}
    scoped_generation = bool(outline_scope.get("arc_title"))
    if existing_text.strip() and not scoped_generation:
        if chapter_contract:
            return existing_text + "\n\n" + chapter_contract_service.render_contract(chapter_contract)
        return existing_text

    if scoped_generation:
        # A regeneration must not feed a previously contaminated chapter back
        # as authoritative context. The chapter contract and current-arc
        # memory below are sufficient to create a clean replacement.
        existing_text = ""

    # M1：注入分层记忆（卷摘要 + 近 5 章摘要 + 上章结尾 + 已完成事实），替代单链 previous_summary
    if memory_context and not _memory_context_is_empty(memory_context):
        return _build_memory_context_before(
            project_id=project_id,
            chapter_id=chapter_id,
            premise=premise,
            chapter=chapter,
            chapter_index=chapter_index,
            total_chapters=total_chapters,
            outline_summary=outline_summary,
            memory_context=memory_context,
            chapter_contract=chapter_contract,
        )

    # 兼容路径：无分层记忆（老数据 / 起始章节）时沿用单链摘要
    if chapter_index == 0:
        stage = "开篇建立悬念"
    elif chapter_index >= total_chapters - 2:
        stage = "尾声与回收"
    elif chapter_index >= max(total_chapters // 2, 1):
        stage = "高潮推进"
    else:
        stage = "关系与线索铺垫"

    base = "\n".join(
        [
            f"故事 premise：{premise or '延续当前项目既有设定。'}",
            f"当前章节：{chapter['title']}",
            f"章节位置：第 {chapter_index + 1} / {total_chapters} 章，阶段={stage}",
            f"全局大纲摘要：{outline_summary or '按当前大纲自然推进。'}",
            f"章节摘要：{chapter.get('summary') or '根据大纲自然推进剧情。'}",
            f"核心冲突：{chapter.get('conflict') or '人物目标与外部阻力发生正面冲突。'}",
            f"揭示信息：{chapter.get('revelation') or '本章需要给出新的线索或后果。'}",
            f"情绪基调：{chapter.get('emotion') or '紧张'}",
            f"上一章已知内容：{previous_summary or '这是本书的起始章节。'}",
        ]
    )
    return base + ("\n\n" + chapter_contract_service.render_contract(chapter_contract) if chapter_contract else "")


def _fit_budget(text: str, budget: int) -> str:
    """按字符预算裁剪文本块；超限时从尾部截断（保留开头信息量更高的部分）。"""
    if not text:
        return ""
    return text if len(text) <= budget else text[: budget - 3] + "..."


def _build_memory_context_before(
    *,
    project_id: str = "",
    chapter_id: str = "",
    premise: str,
    chapter: dict[str, Any],
    chapter_index: int,
    total_chapters: int,
    outline_summary: str,
    memory_context: dict[str, Any],
    chapter_contract: dict[str, Any] | None = None,
) -> str:
    """分层记忆注入：卷摘要 + 最近章节摘要 + 上一章结尾 + 实体状态 + 相关历史
    + 已完成事实。

    记忆升级 v1（预算改造）：替代原先依赖下游 `contextBefore[-4000:]` 的隐式截断。
    各分块按显式预算分配（_CONTEXT_BUDGET_*），低优先级分块（FTS/卷摘要）先被裁剪，
    高优先级分块（milestones 已完成事实、entity_state 实体状态）在预算内优先保留，
    保证长书场景下不可逆事实与角色状态不被整体丢弃。

    BUG-3（Codex 复测）：召回日志必须记录**裁剪后**的真实注入内容，而非构建 dict 时
    的原始长度——否则日志会出现"已注入"但 prompt 实际已裁掉的假阳性。
    """
    outline_scope = ((chapter_contract or {}).get("arc_context") or {}).get("outline_scope") or {}
    if chapter_index == 0:
        stage = "开篇建立悬念"
    elif chapter_index >= total_chapters - 2:
        stage = "尾声与回收"
    elif chapter_index >= max(total_chapters // 2, 1):
        stage = "高潮推进"
    else:
        stage = "关系与线索铺垫"

    volume_summaries = memory_context.get("volume_summaries") or []
    recent_summaries = memory_context.get("recent_chapter_summaries") or []
    last_ending = memory_context.get("last_chapter_ending") or ""
    entity_state = memory_context.get("entity_state") or ""
    fts_history = memory_context.get("fts_history") or ""
    milestones = memory_context.get("completed_milestones") or ""
    active_foreshadows = memory_context.get("active_foreshadows") or ""
    timeline = memory_context.get("timeline") or ""

    if outline_scope.get("arc_title"):
        # These blocks are global or future-bearing and have no reliable arc
        # identity at this layer. The current chapter contract is the source
        # of truth; only same-arc recent summaries/ending may remain.
        volume_summaries = []
        entity_state = ""
        fts_history = ""
        milestones = ""
        active_foreshadows = ""
        timeline = ""
    header = "\n".join(
        [
            f"故事 premise：{_fit_budget(premise or '延续当前项目既有设定。', CONTEXT_BUDGET_PREMISE)}",
            f"当前章节：{chapter['title']}",
            f"章节位置：第 {chapter_index + 1} / {total_chapters} 章，阶段={stage}",
            f"全局大纲摘要：{_fit_budget(outline_summary or '按当前大纲自然推进。', 240)}",
            f"章节摘要：{_fit_budget(chapter.get('summary') or '根据大纲自然推进剧情。', 200)}",
            f"核心冲突：{_fit_budget(chapter.get('conflict') or '人物目标与外部阻力发生正面冲突。', 200)}",
            f"揭示信息：{_fit_budget(chapter.get('revelation') or '本章需要给出新的线索或后果。', 200)}",
            f"情绪基调：{_fit_budget(chapter.get('emotion') or '紧张', 80)}",
        ]
    )[:CONTEXT_BUDGET_HEADER]

    if chapter_contract:
        header += "\n" + _fit_budget(
            chapter_contract_service.render_contract(chapter_contract), 1800
        )
    if outline_scope.get("arc_title"):
        header += "\n当前剧情弧隔离：只允许当前弧已发生内容，不得出现或暗示其他弧。"

    # 低优先级块先加入（预算充足时全保留，不足时逐块让位给高优先级）
    memory_blocks: list[dict[str, Any]] = []
    if volume_summaries:
        vol_lines = ["已完结卷摘要："]
        vol_lines.extend(f"- {summary}" for summary in volume_summaries)
        vol_text = "\n".join(vol_lines)
        memory_blocks.append(
            {"key": "volume_summaries", "text": _fit_budget(vol_text, CONTEXT_BUDGET_VOLUME)}
        )
    if recent_summaries:
        recent_lines = ["最近章节摘要："]
        recent_lines.extend(f"- {summary}" for summary in recent_summaries)
        recent_text = "\n".join(recent_lines)
        memory_blocks.append(
            {"key": "recent_chapter_summaries", "text": _fit_budget(recent_text, CONTEXT_BUDGET_RECENT)}
        )
    if last_ending:
        memory_blocks.append(
            {"key": "last_chapter_ending", "text": "上一章结尾：" + _fit_budget(last_ending, CONTEXT_BUDGET_ENDING)}
        )
    else:
        memory_blocks.append(
            {"key": "last_chapter_ending", "text": "上一章已知内容：这是本书较早的章节。"}
        )
    # 高优先级：实体状态 + 相关历史（FTS）
    if entity_state:
        memory_blocks.append(
            {"key": "entity_state", "text": _fit_budget(entity_state, CONTEXT_BUDGET_ENTITY)}
        )
    if fts_history:
        memory_blocks.append(
            {"key": "fts_history", "text": _fit_budget(fts_history, CONTEXT_BUDGET_FTS)}
        )
    # 阶段C：未回收伏笔（低优先级）
    if active_foreshadows:
        memory_blocks.append(
            {"key": "active_foreshadows", "text": _fit_budget(active_foreshadows, CONTEXT_BUDGET_FORESHADOW)}
        )
    # 阶段C：时间线约束（低优先级）
    if timeline:
        memory_blocks.append(
            {"key": "timeline", "text": _fit_budget(timeline, CONTEXT_BUDGET_TIMELINE)}
        )
    # 最高优先级：已完成事实置于注入块末尾（下游兜底窗口内保留）
    if milestones:
        memory_blocks.append(
            {"key": "completed_milestones", "text": _fit_budget(milestones, CONTEXT_BUDGET_MILESTONES)}
        )

    # 按优先级从低到高丢弃整块，直到满足总预算（entity_state / completed_milestones 永不丢弃）
    _DROP_ORDER = (
        "active_foreshadows",
        "timeline",
        "volume_summaries",
        "recent_chapter_summaries",
        "last_chapter_ending",
        "fts_history",
    )

    def _injected_total_len(blocks: list[dict[str, Any]]) -> int:
        total = len(header)
        for block in blocks:
            total += len(block["text"]) + 1  # +1 = 换行
        return total

    for drop_key in _DROP_ORDER:
        if _injected_total_len(memory_blocks) <= CONTEXT_BUDGET_TOTAL:
            break
        memory_blocks = [block for block in memory_blocks if block["key"] != drop_key]

    lines = [header] + [block["text"] for block in memory_blocks]
    joined = "\n".join(lines)
    # 极端兜底：仅剩核心块（header + entity_state + milestones）仍超限时，
    # 必须保留尾部（milestones 最高优先级绝不能被裁掉），从头部裁剪元数据。
    if len(joined) > CONTEXT_BUDGET_TOTAL:
        joined = joined[-(CONTEXT_BUDGET_TOTAL - 4):].lstrip()
        joined = "…（前文略）\n" + joined

    # BUG-3 + 验收 P1-2：召回日志只记录**裁剪后最终实际注入**的块——
    # 以"块文本完整存在于最终 prompt"为准（极端兜底截断的块不虚报），
    # 保证日志与最终 prompt 一致（M1/M8 实测可信，日志无假阳性）。
    injected_blocks: list[dict[str, Any]] = []
    for block in memory_blocks:
        if block["text"] and block["text"] in joined:
            if block["key"] == "completed_milestones":
                block = {**block, "source_ids": memory_service.milestone_source_ids(project_id)}
            injected_blocks.append(block)

    if project_id and chapter_id:
        _log_memory_recall(project_id, chapter_id, injected_blocks)
    return joined


def _memory_context_is_empty(memory_context: dict[str, Any]) -> bool:
    """分层记忆是否为空（全空时回退单链摘要，兼容老数据）。"""
    return not any(
        (
            memory_context.get("volume_summaries"),
            memory_context.get("recent_chapter_summaries"),
            memory_context.get("last_chapter_ending"),
            memory_context.get("completed_milestones"),
            memory_context.get("entity_state"),
            memory_context.get("fts_history"),
            memory_context.get("active_foreshadows"),
            memory_context.get("timeline"),
        )
    )


# 记忆升级 v1：FTS 检索关键词过滤（章节编号等无信息量词）
_FTS_STOPWORDS = {
    "第一章", "第二章", "第三章", "第四章", "第五章", "第六章", "第七章", "第八章",
    "第九章", "第十章", "间章", "序章", "尾声", "番外", "人物", "剧情", "故事",
    "章节", "标题", "内容", "之前", "之后", "开始", "出现", "进行", "没有",
}


def _fts_keywords_from_chapter(chapter: dict[str, Any]) -> list[str]:
    """从当前章标题/摘要/冲突/揭示信息提取 3 字以上中文关键词（FTS trigram 下限）。

    过滤章节编号等无信息量词，去重后取前 5 个；无有效关键词返回空列表
    （search_related_history 据此跳过注入）。绝不抛异常。
    """
    raw = " ".join(
        [
            chapter.get("title") or "",
            chapter.get("summary") or "",
            chapter.get("conflict") or "",
            chapter.get("revelation") or "",
        ]
    )
    pieces = re.findall(r"[\u4e00-\u9fff]{3,}", raw)
    # P1-3：长连续汉字片段（>8 字）优先切 4 字滑窗——FTS5 unicode61 对中文
    # 整段 token 无法短语子串匹配，切窗后关键词粒度细、召回率更高；
    # 短片段（≤8 字）最后补充（LIKE 兜底子串匹配同样受益）。
    long_pieces = [p for p in pieces if len(p) > 8]
    short_pieces = [p for p in pieces if len(p) <= 8]
    keywords: list[str] = []

    def _add(kw: str) -> None:
        if not kw or kw in _FTS_STOPWORDS or kw in keywords:
            return
        keywords.append(kw)

    for piece in long_pieces:
        for i in range(0, len(piece) - 3):
            _add(piece[i:i + 4])
    for piece in short_pieces:
        _add(piece)
    return keywords[:8]


def _log_memory_recall(
    project_id: str, chapter_id: str, injected_blocks: list[dict[str, Any]]
) -> None:
    """把**最终实际注入**的各记忆块写入 memory_recall_log（验收 M1/M8 实测）。

    只读取 injected_blocks（由 _build_memory_context_before 在预算裁剪后生成，
    每块文本保证完整存在于最终 prompt），不再使用裁剪前的原始候选块——
    消除"日志已注入但 prompt 实际已裁掉"的假阳性（验收 P1-2 修复）。
    任何异常静默跳过，绝不阻塞生成任务。
    """
    try:
        for block in injected_blocks:
            text = block.get("text") or ""
            if not text:
                continue
            memory_recall_dao.log_recall(
                project_id,
                chapter_id,
                block_key=block.get("key") or block.get("block_key") or "",
                source_ids=block.get("source_ids") or [],
                chars=len(text),
            )
    except Exception:  # noqa: BLE001  日志写入失败降级，不阻塞生成
        pass


def _build_memory_context(
    *,
    project_id: str,
    chapter_nodes: list[dict[str, Any]],
    current_chapter_id: str,
) -> dict[str, Any]:
    """构建分层记忆上下文：最近 N 卷摘要 + 最近章节摘要 + 上一章结尾 + 已完成事实
    + 实体当前状态 + 相关历史摘要（记忆升级 v1），并记录实际注入内容到召回日志。"""
    current_index: int | None = None
    for index, node in enumerate(chapter_nodes):
        if node["id"] == current_chapter_id:
            current_index = index
            break

    previous_id = None
    if current_index is not None and current_index > 0:
        previous_id = chapter_nodes[current_index - 1]["id"]

    last_ending = ""
    if previous_id:
        last_ending = _load_existing_chapter_text(project_id, previous_id)[
            -memory_service.LAST_ENDING_LENGTH:
        ]

    # P1-2：只注入最近 N 卷摘要（早期卷摘要易挤占下游保留窗口）
    has_arc_scope = bool(
        (chapter_nodes[current_index].get("arcId") if current_index is not None else "")
    )
    volume_summaries = [] if has_arc_scope else volumes_dao.list_volume_summaries(project_id)[
        -memory_service.RECENT_VOLUME_SUMMARIES:
    ]

    # BUG-B：大纲 order 为卷内序号（跨卷重复），`before_chapter_order` 过滤失效；
    # 改按全局有序 chapter_nodes 取当前章之前的章节摘要（倒序找最近 N 个有摘要的）。
    recent_summaries: list[str] = []
    preceding = chapter_nodes[max(0, (current_index or 0) - 60) : current_index]
    if has_arc_scope:
        current_arc_id = chapter_nodes[current_index].get("arcId")
        preceding = [node for node in preceding if node.get("arcId") == current_arc_id]
    if preceding:
        summaries_map = {
            item["chapter_id"]: item["summary"]
            for item in chapters_dao.list_summaries_by_ids(
                project_id, [n["id"] for n in preceding]
            )
        }
        for node in reversed(preceding):
            summary = summaries_map.get(node["id"], "")
            if summary:
                recent_summaries.append(summary)
                if len(recent_summaries) >= memory_service.RECENT_CHAPTER_SUMMARIES:
                    break

    # 记忆升级 v1：实体当前状态块（人物/地点/物品/阵营/世界规则）
    entity_state = memory_entities_dao.build_entity_state_block(project_id)
    # 记忆升级 v1：相关历史摘要（FTS 检索，排除当前章及其后章节防剧透）
    fts_history = ""
    if current_index is not None:
        exclude_ids = [n["id"] for n in chapter_nodes[current_index:]][:400]
        fts_history = memory_service.search_related_history(
            project_id,
            keywords=_fts_keywords_from_chapter(chapter_nodes[current_index]),
            exclude_chapter_ids=exclude_ids,
        )

    # 阶段C：未回收伏笔（planted/progressing，最近 8 条）——长书连续性关键资产
    # 已知当前章序号时，顺带标注超期伏笔（零额外 LLM 调用）
    active_foreshadows = (
        ""
        if has_arc_scope
        else _build_active_foreshadows(
            project_id,
            current_order=(current_index + 1) if current_index is not None else None,
        )
    )
    # 阶段C：时间线约束（当前章节位置 + 最近状态变化事实，防时间/状态错乱）
    timeline = "" if has_arc_scope else _build_timeline(project_id, current_index, len(chapter_nodes))
    # P2：作者显式启用的「参考写法」（可多份，按强度；默认不启用；只作表达参考，不覆盖本书风格与设定）
    reference_style, _reference_used = reference_work_service.render_reference_blocks(project_id)

    memory_context = {
        "volume_summaries": volume_summaries,
        "recent_chapter_summaries": recent_summaries,
        "last_chapter_ending": last_ending,
        "completed_milestones": "" if has_arc_scope else memory_service.build_completed_milestones(project_id),
        "entity_state": "" if has_arc_scope else entity_state,
        "fts_history": "" if has_arc_scope else fts_history,
        "active_foreshadows": active_foreshadows,
        "reference_style": reference_style,
        "timeline": timeline,
    }
    return memory_context


def _context_reference_summary(
    *, characters: list[dict[str, Any]], memory_context: dict[str, Any],
    style_contract: dict[str, Any] | None, project_id: str,
) -> str:
    """Return counts from the exact assembled inputs, for a user-facing job summary."""
    del project_id  # Kept in the call signature for compatibility with both job paths.
    context = memory_context or {}
    state_count = sum(
        1 for key, value in context.items()
        if key != "active_foreshadows" and value not in (None, "", [], {})
    )
    foreshadow_text = str(context.get("active_foreshadows") or "")
    foreshadow_count = max(0, sum(1 for line in foreshadow_text.splitlines() if line.strip().startswith("- ")))
    style_count = 1 if style_contract else 0
    return f"本次生成已参考：人物 {len(characters)}、故事状态 {state_count}、伏笔 {foreshadow_count}、当前文风 {style_count}"


def _build_active_foreshadows(
    project_id: str,
    limit: int = 8,
    current_order: int | None = None,
) -> str:
    """阶段C：组装「未回收伏笔」注入块（planted/progressing，时间倒序）。

    传入 current_order（当前章序号）时，额外标注已超期伏笔并追加一句超期提醒
    （复用 foreshadow_service 的超期判定，零额外 LLM 调用）。
    """
    try:
        items = foreshadows_dao.list_active_foreshadows(project_id, limit=limit)
        if not items:
            return ""
        overdue_map: dict[str, dict[str, Any]] = {}
        if current_order:
            overdue_map = {
                entry["name"]: entry
                for entry in foreshadow_service.overdue_active_foreshadows(project_id, current_order)
            }
        lines = ["【未回收伏笔】以下伏笔尚未回收，写作时保持悬念并适时推进："]
        for item in items:
            name = (item.get("name") or "").strip()
            desc = (item.get("description") or "").strip()
            if not name:
                continue
            line = f"- {name}"
            overdue = overdue_map.get(name)
            if overdue:
                line += f"（⚠ 已超期 {overdue['overdue_by']} 章，预计第 {overdue['target_order']} 章回收）"
            if desc:
                line += f"：{desc[:60]}"
            lines.append(line)
        if overdue_map:
            lines.append(
                "⚠ 上面标 ⚠ 的伏笔已超过预计回收章节，本章如剧情允许，应优先给出推进或回收线索。"
            )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001  降级：任何异常返回空块，不阻塞生成
        return ""


def _build_timeline(project_id: str, current_index: int | None, total_chapters: int) -> str:
    """阶段C：组装「时间线约束」注入块。

    由当前章节进度（第 N / M 章）与最近 N 条状态变化事实构成，提示模型
    遵守既有时序、不要把已完成剧情当成未发生（防时间线错乱）。
    """
    try:
        position = f"当前剧情进度：第 {current_index + 1} / {total_chapters} 章。"
        if current_index is None:
            position = "当前剧情进度：全章创作（序章起）。"
        recent = novel_facts_dao.list_recent_state_changed_facts(project_id, limit=3)
        lines = ["【时间线约束】请保持叙事时间顺序与已发生事实一致：", position]
        for fact in recent:
            content = (fact.get("content") or fact.get("fact") or "").strip()
            if content:
                lines.append(f"- 已发生：{content[:70]}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001  降级：任何异常返回空块，不阻塞生成
        return ""


def _build_direction(chapter: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"请直接写出完整小说正文《{chapter['title']}》。",
            "不要输出提纲、解释、项目符号或创作说明。",
            "保持叙事连贯，让冲突真实发生，让人物行动推动剧情。",
            f"本章要重点体现：{chapter.get('revelation') or '新的线索或后果'}。",
            f"结尾情绪建议：{chapter.get('emotion') or '紧张'}，并为下一章留下推进空间。",
        ]
    )


def _build_expansion_direction(
    *,
    chapter: dict[str, Any],
    actual_words: int,
    target_words: int,
    expansion_pass: int,
    chapter_contract: dict[str, Any] | str = "",
) -> str:
    remaining = max(target_words - actual_words, 0)
    lines = [
            f"请继续扩写《{chapter['title']}》的后续正文，补足约 {remaining} 字内容。",
            "只输出可直接并入章节的小说正文，不要重复前文，不要重写开头，不要输出说明。",
            "优先补足场景推进、人物反应、动作后果、信息揭示、情绪递进与章节收束。",
            f"本次是补写第 {expansion_pass} 轮，请确保新增内容承接已有段落并自然推进。",
            f"继续围绕本章重点：{chapter.get('revelation') or '推进冲突并释放新的线索或后果'}。",
        ]
    if chapter_contract:
        lines.append("当前 Chapter Contract（必须遵守，不得重启完整剧情）：")
        lines.append(
            chapter_contract_service.render_contract(chapter_contract)
            if isinstance(chapter_contract, dict)
            else chapter_contract
        )
    return "\n".join(lines)


async def _persist_chapter_and_sync_memory(
    *,
    job: NovelGenerationJob,
    chapter: dict[str, Any],
    result: dict[str, Any],
    outline: dict[str, Any],
) -> str:
    """落库章节正文并同步 M1 记忆（章摘要 → 事实账本 → 实体 → 卷摘要），返回本章摘要。

    BUG-A（记忆升级 v1）：fallback/降级生成（result["used_fallback"]=True）的占位文本
    不得写入章节摘要——占位文本会污染后续记忆注入（近章摘要/卷摘要全被模板句子覆盖）。
    fallback 时保留原 summary（或为空），待后续正常生成再补摘要。
    LLM 记忆同步全部降级（内部捕获），绝不抛出异常阻塞整书任务。
    """
    content = result.get("content") or ""
    if result.get("generation_incomplete") or not content.strip():
        raise GenerationIncompleteError("生成结果不完整或为空，禁止提交章节与故事投影。")
    story_state = story_state_service.ensure_story_state(job.project_id)
    contract = chapter_contracts_dao.get_latest_contract(job.project_id, chapter["id"])
    observed = story_state_transition_service.extract_observed_state(content, contract or {})
    evidence = {
        "observed_facts": observed.get("knowledge") or [],
        "state_changes": observed.get("state_changes") or [],
        "relationship_changes": observed.get("relationships") or [],
        "knowledge_changes": observed.get("knowledge") or [],
        "conflict_changes": observed.get("conflicts") or [],
        "foreshadow_changes": observed.get("foreshadows") or [],
        "payoffs": observed.get("foreshadows") or [],
        "unresolved_questions": observed.get("mysteries") or [],
        "next_direction": (observed.get("next_goals") or [""])[0],
    }
    # P1（Codex 复测）：正文落库与不可变 commit 记录在同一事务内原子写入——
    # commit 写入失败视为阻断性质量债务（抛异常 → 正文不落库 → 任务失败待人工）。
    _persist_generated_chapter(
        job.project_id,
        chapter,
        content,
        commit_meta={
            "content_hash": chapter_commits_dao.sha256_hex(content),
            "context_hash": result.get("context_hash") or "",
            "audit_id": result.get("audit_id") or "",
            "provider": result.get("provider") or "",
            "model": result.get("model") or "",
            "contract_version": int((contract or {}).get("version") or 0),
            "story_state_version_before": int(story_state.get("version") or 0),
            "created_by": "generation",
            "qa_completed": not bool(result.get("qa_incomplete")),
            "style_plan_id": result.get("style_plan_id"),
            "style_plan_revision": result.get("style_plan_revision"),
            **evidence,
        },
    )

    # P2 伏笔账本：本章落库后自动追踪活跃伏笔（planted → progressing → resolved/abandoned），
    # 让长程伏笔不再依赖人工维护。与记忆同步同样的纪律：失败只跳过，绝不阻塞整书任务；
    # fallback 占位文本不参与追踪（避免把占位文当成正式剧情推进）。
    # 开关 FORESHADOW_AUTO_TRACK（默认开）：每章会多花 1 次项目每日 AI 额度，
    # 额度紧张时置 false 即可关掉（伏笔状态届时只能人工维护）。
    if not result.get("used_fallback") and settings.foreshadow_auto_track:
        try:
            await foreshadow_service.update_foreshadows_after_chapter(
                job.project_id,
                chapter["id"],
                int(chapter.get("order") or chapter.get("chapter_order") or 0),
                content,
                outline,
            )
        except Exception:  # noqa: BLE001  追踪失败不影响正文与记忆
            pass

    # BUG-A：fallback 占位文本不落库为摘要，避免污染记忆上下文
    if result.get("used_fallback"):
        # 仍要同步 FTS（正文已落库，摘要不变），保证新正文可被检索
        try:
            memory_fts_dao.sync_chapter_fts(job.project_id, chapter["id"])
            chapter_commits_dao.set_projection(
                project_id=job.project_id, chapter_id=chapter["id"],
                projection_type="fts", status="done",
            )
        except Exception:  # noqa: BLE001  FTS 同步失败降级，不阻塞
            chapter_commits_dao.set_projection(
                project_id=job.project_id, chapter_id=chapter["id"],
                projection_type="fts", status="failed",
            )
        return chapters_dao.get_chapter_summary(job.project_id, chapter["id"]) or ""

    # Phase 5: formal commits use deterministic projections only.  Each writer
    # is independently logged and retryable from the accepted commit.
    accepted = chapter_commits_dao.get_commit(job.project_id, chapter["id"])
    with get_conn() as _phase6_conn:
        phase6_schema = bool(_phase6_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chapter_digests'").fetchone())
    if accepted and phase6_schema:
        projection_results = []
        for writer in projection_runner_service.WRITERS:
            projection_results.append(projection_runner_service.run_projection(accepted["id"], writer))
        if any(item.get("status") == "failed" for item in projection_results):
            job.status = "completed_with_projection_debt"
            job.message = "chapter accepted; deterministic projection debt remains"
        return chapters_dao.get_chapter_summary(job.project_id, chapter["id"]) or ""

    # Legacy compatibility path for callers that did not create a commit.
    try:
        summary = await memory_service.summarize_chapter(content, chapter.get("title", ""))
        if summary:
            chapters_dao.update_chapter_summary(job.project_id, chapter["id"], summary)
        chapter_commits_dao.set_projection(
            project_id=job.project_id, chapter_id=chapter["id"],
            projection_type="chapter_summary", status="done" if summary else "skipped",
        )
    except Exception:  # noqa: BLE001  摘要失败降级（单项投影 failed，可重放）
        chapter_commits_dao.set_projection(
            project_id=job.project_id, chapter_id=chapter["id"],
            projection_type="chapter_summary", status="failed",
        )
        summary = ""

    _proj_steps = (
        ("facts", memory_service.extract_facts, (job.project_id, chapter["id"], content)),
        ("entities", memory_service.extract_entities, (job.project_id, chapter["id"], content)),
        ("volume", memory_service.record_chapter_completed, (job.project_id, outline, chapter["id"])),
    )
    for ptype, fn, args in _proj_steps:
        try:
            await fn(*args)
            chapter_commits_dao.set_projection(
                project_id=job.project_id, chapter_id=chapter["id"],
                projection_type=ptype, status="done",
            )
        except Exception as exc:  # noqa: BLE001  单项投影失败降级，可单项重放
            chapter_commits_dao.set_projection(
                project_id=job.project_id, chapter_id=chapter["id"],
                projection_type=ptype, status="failed", detail=str(exc)[:200],
            )
    # FTS 为同步投影
    try:
        memory_fts_dao.sync_chapter_fts(job.project_id, chapter["id"])
        chapter_commits_dao.set_projection(
            project_id=job.project_id, chapter_id=chapter["id"],
            projection_type="fts", status="done",
        )
    except Exception as exc:  # noqa: BLE001  单项投影失败降级，可单项重放
        chapter_commits_dao.set_projection(
            project_id=job.project_id, chapter_id=chapter["id"],
            projection_type="fts", status="failed", detail=str(exc)[:200],
        )

    # P0-E：章节完成后推进 Story State。新库中幂等落盘；老库/旧 fixture
    # 尚未迁移时跳过，不改变既有正文与记忆投影语义。
    try:
        transition = story_state_transition_service.apply_chapter_transition(
            job.project_id, chapter["id"], summary=summary or "", content=content
        )
        if isinstance(transition, dict) and transition.get("status") == "conflict":
            raise RuntimeError(
                f"章节 {chapter['title']} 产生 Story State Conflict，等待人工确认后再继续。"
            )
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001  状态转移是可重放投影，不阻塞正文落库
        pass

    return summary


async def replay_memory_projection(
    *,
    project_id: str,
    chapter_id: str,
    projection_type: str,
    outline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retry only from the accepted commit; old names are input adapters.

    This endpoint retains legacy request vocabulary for older UI callers, but
    it must never retry the former LLM-backed memory APIs.
    """
    canonical = projection_runner_service.LEGACY_WRITER_NAMES.get(projection_type, projection_type)
    if canonical not in projection_runner_service.WRITERS:
        return {"status": "failed", "detail": f"未知投影类型：{projection_type}", "projection_type": projection_type}
    commit = chapter_commits_dao.get_canonical_commit(project_id, chapter_id)
    if not commit:
        if not (chapters_dao.get_chapter_content(project_id, chapter_id) or "").strip():
            chapter_commits_dao.set_projection(
                project_id=project_id, chapter_id=chapter_id,
                projection_type=canonical, status="skipped", detail="章节无正文",
            )
            return {"status": "skipped", "detail": "章节无正文", "projection_type": projection_type, "chapter_id": chapter_id}
        return {"status": "failed", "detail": "canonical commit not found", "projection_type": projection_type}
    result = projection_runner_service.run_projection(commit["id"], canonical, retry_of="legacy_retry_adapter")
    result["projection_type"] = projection_type
    return result


def _persist_generated_chapter(
    project_id: str,
    chapter: dict[str, Any],
    content: str,
    commit_meta: dict[str, Any] | None = None,
) -> None:
    """落库章节正文，并在**同一事务**内追加不可变 commit（P1 Codex 复测）。

    - 正文保存 + chapter_commit_history 追加在同一 conn/事务：任一失败整体回滚，
      避免"正文已变但审计缺失"。
    - commit 写入失败视为阻断性质量债务（抛异常 → 正文不落库 → 任务失败待人工），
      而非静默吞掉。
    """
    today = utc_today()
    # 正文首行写「第N章 章节名」（幂等；标题里没有真名时原样返回，不影响老项目）。
    content = ensure_chapter_heading(content, chapter.get("title"))
    word_count = _count_words(content)

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        style_transition_service.validate_plan_on_conn(
            conn, project_id, (commit_meta or {}).get("style_plan_id"),
            (commit_meta or {}).get("style_plan_revision"),
        )
        existing = conn.execute(
            "SELECT id FROM chapters WHERE id = ? AND project_id = ?",
            (chapter["id"], project_id),
        ).fetchone()

        if existing is None:
            chapters_dao._insert_chapter_on_conn(conn, {
                "id": chapter["id"],
                "project_id": project_id,
                "volume_id": chapter.get("parentId") or "",
                "title": repair_text(chapter["title"]),
                "chapter_order": chapter.get("order") or 1,
                "content": content,
                "word_count": word_count,
                "status": "completed",
                "summary": repair_text(chapter.get("summary") or ""),
                "notes": "",
                "created_at": today,
                "updated_at": today,
            })
        else:
            chapters_dao._update_chapter_on_conn(conn, chapter["id"], project_id, {
                "volume_id": chapter.get("parentId") or "",
                "title": repair_text(chapter["title"]),
                "chapter_order": chapter.get("order") or 1,
                "content": content,
                "word_count": word_count,
                "status": "completed",
                "summary": repair_text(chapter.get("summary") or ""),
                "updated_at": today,
            })

        # Phase 5 gate runs before a formal immutable commit.  For generated
        # chapter shells, create a deterministic contract in this transaction.
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        contract_row = conn.execute(
            "SELECT version FROM chapter_contracts WHERE project_id = ? AND chapter_id = ? ORDER BY version DESC LIMIT 1",
            (project_id, chapter["id"]),
        ).fetchone() if "chapter_contracts" in tables else None
        if contract_row is None and "chapter_contracts" in tables:
            contract_version = 1
            conn.execute(
                """INSERT INTO chapter_contracts
                   (id, project_id, chapter_id, version, status, goal, conflict, state_change,
                    characters_json, key_events_json, must_payoff_json, must_avoid_json,
                    ending_hook, pacing, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'compiled', ?, '', '', '[]', '[]', '[]', '[]', '', '', ?, ?)""",
                (gen_id(), project_id, chapter["id"], contract_version,
                 repair_text(chapter.get("title") or "advance story"), today, today),
            )
        elif contract_row is not None:
            contract_version = int(contract_row["version"] or 1)
        else:
            contract_version = 0
        if commit_meta and "chapter_contracts" in tables:
            conflict_count = int(conn.execute(
                "SELECT COUNT(*) AS n FROM novel_facts WHERE project_id = ? AND is_conflict = 1", (project_id,)
            ).fetchone()["n"])
            gate = write_gate_service.evaluate_precommit(
                project_id, chapter["id"], content, expected_version=contract_version,
                qa_completed=bool(commit_meta.get("qa_completed", True)),
                contract_override={"version": contract_version},
                story_state_available=bool(commit_meta.get("story_state_version_before", 0)),
                conflict_count=conflict_count,
            )
            if not gate["allowed"]:
                raise RuntimeError("precommit gate blocked: " + ", ".join(item["code"] for item in gate["blockers"]))

        # P1：同事务写不可变 commit（正文+commit 原子一致，失败即回滚）
        commit_id = ""
        if commit_meta:
            commit_id = chapter_commits_dao.record_commit_on_conn(
                conn,
                project_id=project_id,
                chapter_id=chapter["id"],
                content_hash=chapter_commits_dao.sha256_hex(content),
                context_hash=commit_meta.get("context_hash") or "",
                audit_id=commit_meta.get("audit_id") or "",
                provider=commit_meta.get("provider") or "",
                model=commit_meta.get("model") or "",
                content_snapshot=content,
                contract_version=contract_version,
                style_profile_version=int(commit_meta.get("style_profile_version") or 0),
                story_state_version_before=int(commit_meta.get("story_state_version_before") or 0),
                observed_facts=commit_meta.get("observed_facts") or [],
                state_changes=commit_meta.get("state_changes") or [],
                relationship_changes=commit_meta.get("relationship_changes") or [],
                knowledge_changes=commit_meta.get("knowledge_changes") or [],
                conflict_changes=commit_meta.get("conflict_changes") or [],
                foreshadow_changes=commit_meta.get("foreshadow_changes") or [],
                payoffs=commit_meta.get("payoffs") or [],
                unresolved_questions=commit_meta.get("unresolved_questions") or [],
                next_direction=commit_meta.get("next_direction") or "",
                created_by=commit_meta.get("created_by") or "generation",
                status=commit_meta.get("status") or "accepted",
            )

        if word_count > 0:
            style_transition_service.record_chapter_completion_on_conn(
                conn,
                project_id,
                chapter["id"],
                plan_id=(commit_meta or {}).get("style_plan_id"),
                expected_revision=(commit_meta or {}).get("style_plan_revision"),
                commit_id=commit_id,
                source="generation",
            )

        total_words = conn.execute(
            "SELECT COALESCE(SUM(word_count), 0) AS total FROM chapters WHERE project_id = ?",
            (project_id,),
        ).fetchone()["total"]
        projects_dao._update_project_word_count_and_status_on_conn(
            conn, project_id, total_words, "drafting", today
        )


def _load_existing_chapter_text(project_id: str, chapter_id: str) -> str:
    return chapters_dao.get_chapter_content(project_id, chapter_id)


def _load_prior_chapter_texts(project_id: str, chapter_id: str) -> list[str]:
    """加载当前章之前所有已落库章节的正文（最多前 8 章，Codex P1-3 跨章重复检测用）。

    按**多卷全局顺序**（卷 order + 卷内 chapter_order）取当前章之前的章节，
    只取有正文的，最多 review_service.AI_CROSS_NGRAM_MAX_PRIOR 章（控制比对成本）。

    Codex P2 复测 P1：不能用 list_chapters（chapter_order 每卷从 1 重新开始，
    会把「第一卷 第1章、第二卷 第1章」交错排列），必须用
    list_chapters_global_order 保证跨卷顺序正确。
    """
    try:
        chapters = chapters_dao.list_chapters_global_order(project_id)
        prior: list[str] = []
        for ch in chapters:
            if ch.get("id") == chapter_id:
                break
            content = (ch.get("content") or "").strip()
            if content:
                prior.append(content)
            if len(prior) >= review_service.AI_CROSS_NGRAM_MAX_PRIOR:
                break
        return prior
    except Exception:  # noqa: BLE001  加载失败降级返回空列表
        return []


def _load_project_word_count(project_id: str) -> int:
    return chapters_dao.aggregate_word_count(project_id)


def _chapter_target_word_count(chapter: dict[str, Any]) -> int:
    return max(int(chapter.get("targetWordCount") or 0), MIN_CHAPTER_WORDS)


def _minimum_acceptable_words(target_words: int) -> int:
    return max(int(target_words * MIN_ACCEPTABLE_RATIO), target_words - 500, MIN_CHAPTER_WORDS)


def _count_words(text: str) -> int:
    # 与前端 `content.replace(/\s/g, '').length` 同口径（见 app.utils.word_count）。
    return count_words(text)


def _normalize_generated_segment(text: str, chapter_title: str) -> str:
    cleaned = repair_text((text or "").strip())
    if not cleaned:
        return ""

    lines = cleaned.splitlines()
    while lines and (
        not lines[0].strip()
        or lines[0].strip().startswith("#")
        or lines[0].strip() == chapter_title
    ):
        lines.pop(0)
    return "\n".join(lines).strip()


def _merge_chapter_content(base: str, addition: str) -> str:
    base_text = base.rstrip()
    add_text = addition.strip()
    if not base_text:
        return add_text
    if not add_text or add_text in base_text:
        return base_text
    return f"{base_text}\n\n{add_text}"


def _summarize(text: str, max_length: int = 220) -> str:
    compact = " ".join(text.split()).strip()
    if not compact:
        return ""
    return compact[:max_length] + ("..." if len(compact) > max_length else "")
