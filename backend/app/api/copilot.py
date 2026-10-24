from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from app.dao import chapter_rewrites as rewrites_dao
from app.dao import chat_conversations as chats
from pydantic import BaseModel

from app.models.schemas import ApiResponse
from app.services import copilot_candidate_service as candidates
from app.services.style_engine.transition_service import TransitionRevisionConflict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])


class CandidateRequest(BaseModel):
    projectId: str
    chapterId: str
    action: str
    selectionText: str = ""
    currentText: str = ""
    instruction: str = ""
    conversationId: Optional[str] = None
    requestId: Optional[str] = None
    regenerateMessageId: Optional[str] = None
    stylePlanId: Optional[str] = None
    stylePlanRevision: Optional[int] = None


class RegenerateRequest(BaseModel):
    instruction: str = ""


@router.post("/candidates")
async def create_candidate(req: CandidateRequest, request: Request):
    require_project_owner(request, req.projectId)
    if req.conversationId:
        turn = chats.begin_turn(req.projectId, req.conversationId, req.requestId,
                                req.instruction or req.action, req.regenerateMessageId, kind='copilot',
                                operation=req.model_dump(exclude={'conversationId', 'requestId', 'regenerateMessageId', 'projectId'}))
        if 'replay' in turn:
            saved = turn['replay']
            if saved.get('mode') == 'failed':
                raise HTTPException(409, saved.get('reply') or '上次请求未完成。')
            return ApiResponse(data={**saved.get('candidate', {}), 'conversationId': req.conversationId,
                                     'assistantMessageId': saved.get('assistantMessageId')})
    try:
        data = await candidates.create_candidate(
            project_id=req.projectId, chapter_id=req.chapterId, action=req.action,
            selection_text=req.selectionText, current_text=req.currentText, instruction=req.instruction,
            style_plan_id=req.stylePlanId, style_plan_revision=req.stylePlanRevision,
        )
    except BaseException as exc:
        if req.conversationId:
            chats.fail_turn(req.conversationId, req.requestId, kind='copilot', interrupted=not isinstance(exc, Exception))
        if isinstance(exc, TransitionRevisionConflict):
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        if isinstance(exc, ValueError):
            # 带机器码的失败（未产生修改 / 模型未跑成）返回结构化 detail，
            # 批量修改可以据此跳过该条而不是把整批停下。
            code = getattr(exc, "copilot_code", "")
            detail: object = {"code": code, "message": str(exc)} if code else str(exc)
            # 秒拒（未调模型）在生产上很难复现，留一条不包含正文的结构化告警，
            # 只记录长度与动作，便于定位“一键修改总是失败”。
            logger.warning(
                "copilot candidate rejected: action=%s code=%s currentText=%d selectionText=%d message=%s",
                req.action, code or "-", len(req.currentText or ""), len(req.selectionText or ""), str(exc),
            )
            raise HTTPException(status_code=400, detail=detail) from exc
        raise
    if req.conversationId:
        saved = chats.finish_turn(req.conversationId, req.requestId, {
            'reply': data.get('explanation') or '已生成候选文本，请确认是否应用。',
            'mode': 'llm', 'candidate': data, 'action': req.action,
        }, kind='copilot')
        data = {**data, 'conversationId': req.conversationId, 'assistantMessageId': saved['assistantMessageId']}
    return ApiResponse(data=data)


@router.get("/candidates")
async def list_candidates(projectId: str, request: Request, chapterId: Optional[str] = None):
    require_project_owner(request, projectId)
    try:
        data = {"records": candidates.list_candidates(projectId, chapterId or "")}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.post("/candidates/{candidate_id}/accept")
async def accept_candidate(candidate_id: str, request: Request):
    record = rewrites_dao.get_rewrite(candidate_id)
    if record:
        require_project_owner(request, record["project_id"])
    try:
        data = candidates.accept_candidate(candidate_id)
    except TransitionRevisionConflict as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not data:
        raise HTTPException(status_code=404, detail="candidate not found")
    return ApiResponse(data=data)


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str, request: Request):
    record = rewrites_dao.get_rewrite(candidate_id)
    if record:
        require_project_owner(request, record["project_id"])
    try:
        data = candidates.reject_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not data:
        raise HTTPException(status_code=404, detail="candidate not found")
    return ApiResponse(data=data)


@router.post("/candidates/{candidate_id}/regenerate")
async def regenerate_candidate(candidate_id: str, request: Request, req: RegenerateRequest | None = None):
    record = rewrites_dao.get_rewrite(candidate_id)
    if record:
        require_project_owner(request, record["project_id"])
    try:
        data = await candidates.regenerate_candidate(candidate_id, (req or RegenerateRequest()).instruction)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not data:
        raise HTTPException(status_code=404, detail="candidate not found")
    return ApiResponse(data=data)
