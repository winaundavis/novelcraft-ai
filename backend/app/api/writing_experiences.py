"""Reviewed writing experience API."""

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel

from app.dao import writing_experiences as experience_dao
from app.models.schemas import ApiResponse
from app.services import writing_experience_service

router = APIRouter(prefix="/projects/{project_id}/writing-experiences", tags=["writing-experiences"])


class ExperienceRequest(BaseModel):
    experience_type: str
    observation: str
    preferred_fix: str = ""
    source_chapters: list[str] = []


@router.get("")
async def list_experiences(project_id: str, request: Request, status: str = ""):
    require_project_owner(request, project_id)
    return ApiResponse(data=experience_dao.list_experiences(project_id, status))


@router.post("")
async def create_experience(project_id: str, data: ExperienceRequest, request: Request):
    require_project_owner(request, project_id)
    try:
        return ApiResponse(data=writing_experience_service.record(project_id, data.experience_type, data.observation, data.preferred_fix, data.source_chapters))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{experience_id}/accept")
async def accept_experience(project_id: str, experience_id: str, request: Request):
    require_project_owner(request, project_id)
    try:
        return ApiResponse(data=writing_experience_service.accept(project_id, experience_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{experience_id}/reject")
async def reject_experience(project_id: str, experience_id: str, request: Request):
    require_project_owner(request, project_id)
    result = experience_dao.reject(experience_id, project_id)
    if not result:
        raise HTTPException(status_code=404, detail="写作经验未找到。")
    return ApiResponse(data=result)
