"""全文问答（建议2：DeepSeek 上下文缓存）· API 端点。

两端点：
- POST /api/projects/{project_id}/fulltext-ask    直灌全书 + 问题 → 回答（含消耗统计）
- GET  /api/projects/{project_id}/fulltext-stats  全书直灌统计（字数 / token / 成本估算）

错误语义：
- 项目不存在 / 无章节 / 无正文 → 400（FulltextEmptyError）
- LLM 未配置或失败 → 200 + degraded 降级提示（不阻塞，不抛错）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel

from app.models.schemas import ApiResponse
from app.services import fulltext_service
from app.services.fulltext_service import FulltextEmptyError


router = APIRouter(prefix="/projects/{project_id}", tags=["fulltext"])


class FulltextAskRequest(BaseModel):
    question: str


@router.post("/fulltext-ask")
async def fulltext_ask(project_id: str, req: FulltextAskRequest, request: Request):
    require_project_owner(request, project_id)
    """直灌全书 + 问题，返回回答与本次消耗统计（DeepSeek 前缀缓存低成本）。"""
    try:
        result = await fulltext_service.ask_fulltext(project_id, req.question)
    except FulltextEmptyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=result)


@router.get("/fulltext-stats")
async def fulltext_stats(project_id: str, request: Request):
    require_project_owner(request, project_id)
    """全书直灌统计：字数 / token 估算 / 成本估算（前端展示"直灌成本约 $X"）。"""
    try:
        result = fulltext_service.fulltext_stats(project_id)
    except FulltextEmptyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=result)
