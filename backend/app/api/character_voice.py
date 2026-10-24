"""Character voice profile API."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel

from app.dao import character_voice as voice_dao
from app.dao import projects as projects_dao
from app.models.schemas import ApiResponse
from app.services import character_voice_service

router = APIRouter(prefix="/projects/{project_id}/character-voices", tags=["character-voices"])


class VoiceProfileUpdate(BaseModel):
    worldview: str | None = None
    speech_length: str | None = None
    sentence_pattern: str | None = None
    favorite_expressions: list[str] | None = None
    avoided_expressions: list[str] | None = None
    emotional_expression: str | None = None
    information_strategy: str | None = None
    behavior_signature: str | None = None


def _project(project_id: str) -> dict[str, Any]:
    project = projects_dao.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目未找到。")
    return project


@router.get("")
async def list_character_voices(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=character_voice_service.get_or_seed_profiles(_project(project_id)))


@router.patch("/{character_id}")
async def update_character_voice(project_id: str, character_id: str, data: VoiceProfileUpdate, request: Request):
    require_project_owner(request, project_id)
    _project(project_id)
    profile = voice_dao.update_profile(project_id, character_id, data.model_dump(exclude_none=True))
    if not profile:
        raise HTTPException(status_code=404, detail="人物声线未找到。")
    return ApiResponse(data=profile)
