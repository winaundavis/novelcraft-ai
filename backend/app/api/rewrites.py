"""文笔局部重写 APIs（文笔质量固化 · Codex 方案执行顺序第 8 步）。

新增路径（保持既有 API 契约不变）：
- GET  /api/rewrites?projectId=&chapterId=      —— 查询重写审计痕迹
- POST /api/rewrites/run                        —— 对给定正文执行评分量表驱动的局部重写链路
- POST /api/rewrites/{rewrite_id}/apply         —— 应用重写（人工确认入口，写 chapters.content）
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel

from app.dao import chapter_rewrites as chapter_rewrites_dao
from app.models.schemas import ApiResponse
from app.services import rewrite_service


router = APIRouter(prefix="/rewrites", tags=["rewrites"])


class RewriteRunRequest(BaseModel):
    projectId: str
    chapterId: str = ""
    text: str
    previousText: str = ""
    isFirstChapter: bool = False


@router.get("")
async def api_list_rewrites(
    projectId: str,
    request: Request,
    chapterId: Optional[str] = None,
    limit: int = 100,
):
    """查询项目（可选限定章节）的重写审计痕迹列表。"""
    require_project_owner(request, projectId)
    records = chapter_rewrites_dao.list_rewrites(
        projectId, chapter_id=chapterId or "", limit=limit
    )
    return ApiResponse(data={"records": records, "count": len(records)})


@router.post("/run")
async def api_run_rewrite_chain(req: RewriteRunRequest, request: Request):
    """对给定正文执行评分量表驱动的局部重写链路（5 种类型 + 审计 + 自动回退）。

    body：{projectId, chapterId, text, previousText?, isFirstChapter?}
    → data：{content, rewrite_count, debt_count, debt_items, rewrites}
    全程降级：LLM 失败返回原正文（degraded 语义由 rewrite_count=0 体现），不抛 5xx。
    """
    require_project_owner(request, req.projectId)
    try:
        result = await rewrite_service.run_style_rewrite_chain(
            project_id=req.projectId,
            chapter_id=req.chapterId,
            content=req.text,
            previous_text=req.previousText,
            is_first_chapter=req.isFirstChapter,
        )
        return ApiResponse(data=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{rewrite_id}/apply")
async def api_apply_rewrite(rewrite_id: str, request: Request):
    """应用重写（人工确认入口）：把审计表里的 rewritten 写入章节正文。

    数据安全：重写服务本身不改 chapters.content；本接口是唯一显式应用入口。
    已被自动回退（reverted）的记录返回 409。
    """
    existing = chapter_rewrites_dao.get_rewrite(rewrite_id)
    if existing:
        require_project_owner(request, existing["project_id"])
    try:
        record = rewrite_service.apply_rewrite(rewrite_id)
        if record:
            # BUG-5：应用后同步长期记忆（FTS/摘要/事实/实体跟进新版本）
            await rewrite_service.apply_rewrite_sync_memory(rewrite_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not record:
        raise HTTPException(status_code=404, detail="重写记录未找到。")
    return ApiResponse(data=record)
