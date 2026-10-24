"""P0-B Story State API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel

from app.dao import story_state_transitions_dao
from app.models.schemas import ApiResponse
from app.services import story_state_service, story_state_transition_service

router = APIRouter(prefix="/projects/{project_id}/story-state", tags=["story-state"])


class StoryStateUpdate(BaseModel):
    currentVolumeGoal: str | None = None
    currentPhaseGoal: str | None = None
    protagonistGoal: str | None = None
    protagonistObstacle: str | None = None
    mainConflict: str | None = None
    subConflicts: list[str] | None = None
    relationshipChanges: list[str] | None = None
    currentMysteries: list[str] | None = None
    activeForeshadows: list[str] | None = None
    unresolvedQuestions: list[str] | None = None
    promisedPayoffs: list[str] | None = None
    lastMajorStateChange: str | None = None
    nextStageDirections: list[str] | None = None


class StoryStateConflictDecision(BaseModel):
    approve: bool


def _to_api(state: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "project_id": "projectId", "current_volume_goal": "currentVolumeGoal",
        "current_phase_goal": "currentPhaseGoal", "protagonist_goal": "protagonistGoal",
        "protagonist_obstacle": "protagonistObstacle", "main_conflict": "mainConflict",
        "sub_conflicts": "subConflicts", "relationship_changes": "relationshipChanges",
        "current_mysteries": "currentMysteries", "active_foreshadows": "activeForeshadows",
        "unresolved_questions": "unresolvedQuestions", "promised_payoffs": "promisedPayoffs",
        "last_major_state_change": "lastMajorStateChange", "next_stage_directions": "nextStageDirections",
        "created_at": "createdAt", "updated_at": "updatedAt",
    }
    return {mapping.get(key, key): value for key, value in state.items() if key != "id"}


@router.get("")
async def get_story_state(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=_to_api(story_state_service.get_story_state(project_id)))


@router.patch("")
async def update_story_state(project_id: str, req: StoryStateUpdate, request: Request):
    require_project_owner(request, project_id)
    mapping = {
        "currentVolumeGoal": "current_volume_goal", "currentPhaseGoal": "current_phase_goal",
        "protagonistGoal": "protagonist_goal", "protagonistObstacle": "protagonist_obstacle",
        "mainConflict": "main_conflict", "subConflicts": "sub_conflicts",
        "relationshipChanges": "relationship_changes", "currentMysteries": "current_mysteries",
        "activeForeshadows": "active_foreshadows", "unresolvedQuestions": "unresolved_questions",
        "promisedPayoffs": "promised_payoffs", "lastMajorStateChange": "last_major_state_change",
        "nextStageDirections": "next_stage_directions",
    }
    fields = {mapping[key]: value for key, value in req.model_dump(exclude_none=True).items()}
    return ApiResponse(data=_to_api(story_state_service.update_story_state(project_id, fields)))


@router.get("/transitions")
async def list_story_state_transitions(project_id: str, request: Request, limit: int = 50):
    require_project_owner(request, project_id)
    return ApiResponse(data=story_state_transitions_dao.list_transitions(project_id, max(1, min(limit, 100))))


@router.post("/transitions/{chapter_id}/resolve")
async def resolve_story_state_transition(project_id: str, chapter_id: str, req: StoryStateConflictDecision, request: Request):
    require_project_owner(request, project_id)
    try:
        result = story_state_transition_service.resolve_state_conflict(project_id, chapter_id, approve=req.approve)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=result)
