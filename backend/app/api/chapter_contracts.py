"""P0-B Chapter Contract read/compile API."""

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner

from app.models.schemas import ApiResponse
from app.services import chapter_contract_service

router = APIRouter(prefix="/projects/{project_id}/chapters", tags=["chapter-contracts"])


@router.get("/{chapter_id}/contract")
async def get_contract(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    try:
        contract = chapter_contract_service.get_or_compile_chapter_contract(project_id, chapter_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=contract)


@router.post("/{chapter_id}/contract/compile")
async def compile_contract(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    try:
        contract = chapter_contract_service.compile_chapter_contract(project_id, chapter_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=contract)
