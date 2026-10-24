"""Read-only long-form readiness dashboard API."""

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner

from app.dao import projects as projects_dao
from app.models.schemas import ApiResponse
from app.services import readiness_service

router = APIRouter(prefix="/projects/{project_id}/readiness", tags=["readiness"])


@router.get("")
async def get_readiness(project_id: str, request: Request):
    require_project_owner(request, project_id)
    project = projects_dao.get_project(project_id)
    return ApiResponse(data=readiness_service.build_readiness(project, project_id))


@router.get("/experiment")
async def get_experiment_readiness(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=readiness_service.experiment_readiness(
        project_id, core_tests_pass=False, default_suite_zero_api=True,
    ))
