"""AI generation APIs."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.dao import projects as projects_dao
from app.llm.audit import query_llm_audit, record_llm_postprocess
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.models.schemas import (
    ApiResponse,
    ChatRequest,
    ContinueWritingRequest,
    DeaiRequest,
    EnhanceRequest,
    GenerateCharacterRequest,
    GenerateOutlineRequest,
)
from app.services.ai_service import (
    _extract_outline_payload,
    chat_reply,
    continue_writing_stream,
    enhance_text,
    generate_character,
    generate_outline,
    prepare_continuation_style,
)
from app.services.deai_service import deai_rewrite


router = APIRouter()


def _raise_ai_http_error(exc: Exception) -> None:
    from app.services.style_engine.transition_service import TransitionRevisionConflict
    if isinstance(exc, TransitionRevisionConflict):
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if isinstance(exc, LLMConfigurationError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, LLMResponseError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/generate-outline")
async def api_generate_outline(req: GenerateOutlineRequest, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, req.projectId)
    try:
        result = await generate_outline(req)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_ai_http_error(exc)


@router.post("/generate-character")
async def api_generate_character(req: GenerateCharacterRequest, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, req.projectId)
    try:
        result = await generate_character(req)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_ai_http_error(exc)


@router.post("/continue-writing")
async def api_continue_writing(req: ContinueWritingRequest, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, req.projectId)
    try:
        # Async-generator bodies start after HTTP headers: validate before SSE
        # to return an actual 409 with zero model calls.
        prepare_continuation_style(req)
        return StreamingResponse(
            continue_writing_stream(req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as exc:
        _raise_ai_http_error(exc)


@router.post("/enhance")
async def api_enhance(req: EnhanceRequest, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, req.projectId)
    try:
        result = await enhance_text(req)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_ai_http_error(exc)


@router.post("/deai")
async def api_deai(req: DeaiRequest, request: Request):
    """去 AI 味后处理：正文 + 项目画像反 AI 规则 + 检测结果 → LLM 重写。

    body：{projectId, text, chapterId?} → data：{result, changes, degraded, scoreBefore, scoreAfter, ...}
    LLM 失败自动降级返回原文（degraded=True），不抛 5xx。
    """
    from app.core.auth import require_project_owner
    require_project_owner(request, req.projectId)
    try:
        result = await deai_rewrite(req.projectId, req.text, req.chapterId)
        return ApiResponse(data=result)
    except Exception as exc:
        _raise_ai_http_error(exc)


@router.post("/chat")
async def api_chat(req: ChatRequest, request: Request):
    from app.core.auth import require_project_owner
    from app.dao import chat_conversations as chats
    import asyncio
    require_project_owner(request, req.projectId)
    if req.conversationId:
        turn = chats.begin_turn(req.projectId, req.conversationId, req.requestId,
                                next((str(m.get('content') or '') for m in reversed(req.messages) if m.get('role') == 'user'), ''),
                                req.regenerateMessageId)
        if 'replay' in turn:
            return ApiResponse(data=turn['replay'])
        req = req.model_copy(update={'messages': turn['history']})
    try:
        result = await chat_reply(req)
        # 大纲修改对话只在模型返回可验证 JSON 时创建候选；普通文本或降级结果绝不写入项目。
        if result.get("intent") == "outline_edit" and result.get("mode") == "llm":
            from app.dao import chapters as chapters_dao
            from app.dao import outline_candidates as candidates_dao
            from app.services.outline_candidate_service import OutlinePatchError, build_outline_diff, describe_model_outline_payload, merge_outline_patch, normalize_model_outline_payload, outline_hash, protected_saved_chapter_removals, validate_outline_patch

            failure_stage = "json_extract"
            response_shape = "unknown"
            parsed_node_count = 0
            trace_id = str(result.get("trace_id") or "")
            try:
                proposed = _extract_outline_payload(str(result.get("reply") or ""))
                if not proposed:
                    raise OutlinePatchError(
                        "模型没有返回可应用的大纲结构。",
                        code="outline_json_missing",
                    )
                response_shape, parsed_node_count = describe_model_outline_payload(proposed)
                failure_stage = "normalize"
                project = projects_dao.get_project(req.projectId)
                if project is None:
                    raise OutlinePatchError("项目不存在。", code="project_missing")
                current_outline = project.get("outline") or {"mainSummary": "", "nodes": []}
                proposed = normalize_model_outline_payload(
                    proposed,
                    current_outline=current_outline,
                )
                response_shape, parsed_node_count = describe_model_outline_payload(proposed)
                if (
                    proposed.get("patchMode") == "merge"
                    and not proposed.get("upsertNodes")
                    and not proposed.get("removeNodeIds")
                    and "mainSummary" not in proposed
                ):
                    raise OutlinePatchError(
                        "模型没有提出任何实际大纲变化。",
                        code="outline_no_changes",
                    )
                failure_stage = "merge"
                proposed = merge_outline_patch(current_outline, proposed)
                failure_stage = "validate"
                project["chapters"] = chapters_dao.list_chapters(req.projectId)
                normalized = validate_outline_patch(
                    project,
                    proposed,
                    allow_saved_chapter_removal=True,
                )
                base_version = max(1, int(current_outline.get("outlineVersion") or 1))
                normalized["outlineVersion"] = base_version + 1
                diff = build_outline_diff(current_outline, normalized)
                if not diff.get("mainSummaryChanged") and not diff.get("added") and not diff.get("removed") and not diff.get("changed") and not diff.get("moved"):
                    raise OutlinePatchError(
                        "模型没有提出任何实际大纲变化。",
                        code="outline_no_changes",
                    )
                failure_stage = "candidate_create"
                user_id = require_project_owner(request, req.projectId)
                candidate = candidates_dao.create_outline_candidate(
                    project_id=req.projectId,
                    user_id=user_id,
                    run_id=str(result.get("run_id") or ""),
                    base_outline_hash=outline_hash(current_outline),
                    base_outline_version=base_version,
                    candidate_outline_json=normalized,
                    previous_outline_json=None,
                    diff_json=diff,
                    instruction=next((str(m.get("content") or "") for m in reversed(req.messages) if m.get("role") == "user"), ""),
                )
                result["candidate"] = {
                    "id": candidate["id"],
                    "projectId": candidate["project_id"],
                    "baseOutlineHash": candidate["base_outline_hash"],
                    "baseOutlineVersion": candidate["base_outline_version"],
                    "candidateOutline": candidate["candidate_outline_json"],
                    "diff": candidate["diff_json"],
                    "instruction": candidate["instruction"],
                    "status": candidate["status"],
                    "requiresConfirmation": bool(protected_saved_chapter_removals(project, normalized)),
                    "protectedRemovals": protected_saved_chapter_removals(project, normalized),
                }
                result["reply"] = "已根据你的要求生成大纲修改候选，请查看差异后决定是否应用。"
                record_llm_postprocess(
                    trace_id,
                    status="completed",
                    stage="candidate_create",
                    response_shape=response_shape,
                    parsed_node_count=parsed_node_count,
                )
            except Exception as exc:
                failure_code = (
                    exc.code
                    if isinstance(exc, OutlinePatchError)
                    else "candidate_create_failed"
                    if failure_stage == "candidate_create"
                    else "outline_field_invalid"
                    if isinstance(exc, (ValueError, TypeError))
                    else "outline_postprocess_internal"
                )
                record_llm_postprocess(
                    trace_id,
                    status="failed",
                    stage=failure_stage,
                    code=failure_code,
                    response_shape=response_shape,
                    parsed_node_count=parsed_node_count,
                )
                result.update({
                    "reply": "模型已返回，但大纲修改未通过安全处理，本次没有应用任何修改。",
                    "mode": "failed",
                    "error_code": "outline_candidate_invalid",
                    "outline_failure_stage": failure_stage,
                    "outline_failure_code": failure_code,
                    "retryable": True,
                    "is_local_suggestion": False,
                })
        if req.conversationId:
            result = chats.finish_turn(req.conversationId, req.requestId, result)
        return ApiResponse(data=result)
    except asyncio.CancelledError:
        if req.conversationId:
            chats.fail_turn(req.conversationId, req.requestId, interrupted=True)
        raise
    except Exception as exc:
        if req.conversationId:
            chats.fail_turn(req.conversationId, req.requestId)
        _raise_ai_http_error(exc)


@router.get("/llm-audit")
async def api_llm_audit(
    request: Request,
    since: str = "",
    minutes: int | None = None,
    projectId: str = "",
    jobId: str = "",
    role: str = "",
    runId: str = "",
    scope: str = "run",
    limit: int = 200,
):
    """LLM 调用审计查询（Codex 六轮 P1 / T5 run 隔离）。

    默认只查**当前 run**（请求头 ``X-NovelCraft-Run-Id`` 或服务端为本次请求生成的 run）。
    历史 run 用显式 ``runId`` 查询；跨 run 的维护者统计必须显式传 ``scope=all``，
    不存在"隐式全表"。
    每行返回 ``run_scope``（current / other / legacy）以区分旧的无 run 关联记录。
    """
    from app.core.auth import require_maintainer
    require_maintainer(request)
    if scope not in {"run", "all"}:
        raise HTTPException(status_code=422, detail="scope 只能是 run 或 all。")
    rows = query_llm_audit(
        since=since or "",
        minutes=minutes,
        project_id=projectId or "",
        job_id=jobId or "",
        role=role or "",
        run_id=runId or "",
        scope=scope,
        limit=limit,
    )
    return ApiResponse(data=rows)


# Codex D3 / P2-3：诊断端点仅在 debug 模式下**注册路由**（而非始终注册 + 404）。
# 非 debug 时路由完全不存在：404 由 FastAPI 路由层直接返回，不会先做请求体校验
# （旧实现始终注册，无效请求会先触发 422 校验，暴露端点存在）。
if settings.debug:

    @router.post("/_diag-continue", include_in_schema=True)
    async def api_diag_continue(req: ContinueWritingRequest):
        """诊断端点（仅 debug 模式注册）：直接检测 generate_continuation_result
        是走真实 API 还是 fallback。生产环境（debug=false）该路由完全不存在。
        """
        from app.services.ai_service import generate_continuation_result

        result = await generate_continuation_result(req)
        return ApiResponse(data={
            "source": result.get("source", "unknown"),
            "provider": result.get("provider", ""),
            "model": result.get("model", ""),
            "text_preview": (result.get("text", "") or "")[:120],
        })
