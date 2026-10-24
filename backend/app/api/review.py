"""Review APIs."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from app.dao import chapters as chapters_dao
from pydantic import BaseModel

from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.models.schemas import ApiResponse
from app.services.review_service import (
    review_chapter_comprehensive,
    review_language,
    review_logic,
)


router = APIRouter(prefix="/review", tags=["review"])


class ReviewRequest(BaseModel):
    projectId: str
    text: str
    chapterId: str = ""
    characters: Optional[list[dict]] = None
    worldRules: Optional[list[dict]] = None


def _raise_review_http_error(exc: Exception) -> None:
    if isinstance(exc, LLMConfigurationError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, LLMResponseError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


def _require_review_scope(req: ReviewRequest, request: Request) -> None:
    require_project_owner(request, req.projectId)
    if req.chapterId and not chapters_dao.get_chapter(req.chapterId, req.projectId):
        raise HTTPException(status_code=404, detail="章节未找到。")


@router.post("/language")
async def api_review_language(req: ReviewRequest, request: Request):
    _require_review_scope(req, request)
    try:
        result = await review_language(req.text, req.chapterId)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_review_http_error(exc)


@router.post("/logic")
async def api_review_logic(req: ReviewRequest, request: Request):
    _require_review_scope(req, request)
    try:
        result = await review_logic(req.text, req.chapterId, req.characters, req.worldRules)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_review_http_error(exc)


@router.post("/comprehensive")
async def api_review_comprehensive(req: ReviewRequest, request: Request):
    _require_review_scope(req, request)
    try:
        result = await review_chapter_comprehensive(req.text, req.chapterId, req.characters, req.worldRules)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_review_http_error(exc)
