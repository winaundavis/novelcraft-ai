import json

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictInt
from typing import Literal

from app.models.schemas import ApiResponse
from app.services.style_engine import transition_service
from app.services.novel_generation_service import (
    get_job,
    get_project_latest_job,
    get_project_running_job,
    list_project_jobs,
    list_recoverable_jobs,
    request_cancel,
    resume_job,
    start_generation_job,
    stream_job_updates,
)


router = APIRouter(tags=["novel-jobs"])


def _encode_sse(payload: dict) -> str:
    return f"event: job\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class StartNovelJobRequest(BaseModel):
    projectId: str
    mode: str = "all"
    currentChapterId: str | None = None
    maxRetries: int = 1
    collaborationMode: Literal["autopilot"] = "autopilot"
    stylePlanId: str | None = None
    stylePlanRevision: StrictInt | None = Field(default=None, ge=0)


@router.post("/start")
async def start_novel_job(req: StartNovelJobRequest, request: Request):
    require_project_owner(request, req.projectId)
    try:
        transition_service.validate_generation_token(req.projectId, req.stylePlanId, req.stylePlanRevision)
        job = start_generation_job(req.projectId, req.mode, req.currentChapterId, req.maxRetries)
    except transition_service.TransitionRevisionConflict as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data={**job.to_payload(), "collaborationMode": req.collaborationMode})


@router.get("/project/{project_id}")
async def get_project_job(project_id: str, request: Request):
    require_project_owner(request, project_id)
    job = get_project_running_job(project_id) or get_project_latest_job(project_id)
    return ApiResponse(data=job.to_payload() if job else None)


@router.get("/project/{project_id}/recoverable")
async def get_project_recoverable_jobs(project_id: str, request: Request, limit: int = 20):
    # Ownership must not depend on whether a latest/running job happens to
    # exist. A failed/interrupted job is still private project data.
    # Preserve the historical empty response for an unknown project.
    from app.dao import projects as projects_dao

    if projects_dao.get_project_owner(project_id) is not None:
        require_project_owner(request, project_id)
    """M2：获取项目下处于可恢复状态（interrupted / 失败章待处理）的任务列表。"""
    jobs = list_recoverable_jobs(project_id, limit=max(1, min(limit, 100)))
    return ApiResponse(data=[job.to_payload() for job in jobs])


@router.get("/project/{project_id}/history")
async def get_project_job_history(project_id: str, request: Request, limit: int = 20):
    require_project_owner(request, project_id)
    jobs = list_project_jobs(project_id, limit=max(1, min(limit, 100)))
    return ApiResponse(data=[job.to_payload() for job in jobs])


@router.get("/{job_id}")
async def get_novel_job(job_id: str, request: Request):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="长篇创作任务未找到。")
    require_project_owner(request, job.project_id)
    return ApiResponse(data=job.to_payload())


@router.get("/{job_id}/events")
async def stream_novel_job(job_id: str, request: Request):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="长篇创作任务未找到。")
    require_project_owner(request, job.project_id)

    async def event_stream():
        async for update in stream_job_updates(job_id):
            if update is None:
                yield ": keepalive\n\n"
                continue
            yield _encode_sse(update.to_payload())

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{job_id}/resume")
async def resume_novel_job(job_id: str, request: Request, skip_failed_chapter: bool = False):
    """M2：从断点恢复长篇创作任务。

    - 默认（skip_failed_chapter=false）：**重试失败章节本身**，保证整书不因
      中间失败永久缺章；末章失败也能重试。
    - skip_failed_chapter=true：用户明确选择跳过失败章，从下一章继续
      （末章失败无法跳过，将返回 400）。
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="长篇创作任务未找到。")
    require_project_owner(request, job.project_id)
    try:
        job = resume_job(job_id, skip_failed_chapter=skip_failed_chapter)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=job.to_payload())


@router.post("/{job_id}/cancel")
async def cancel_novel_job(job_id: str, request: Request):
    existing = get_job(job_id)
    if existing:
        require_project_owner(request, existing.project_id)
    job = request_cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="长篇创作任务未找到。")
    return ApiResponse(data=job.to_payload())
