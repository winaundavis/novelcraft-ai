"""Protected lifecycle endpoints for project outline change candidates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.auth import require_project_owner
from app.dao import chapters as chapters_dao
from app.dao import outline_candidates as candidates_dao
from app.dao import projects as projects_dao
from app.db import get_conn, json_dump
from app.models.schemas import ApiResponse, OutlineCandidateCreate
from app.services.outline_candidate_service import (
    OutlinePatchError,
    build_outline_diff,
    outline_hash,
    protected_saved_chapter_removals,
    validate_outline_patch,
)
from app.services.chapter_trash_service import reconcile_outline_on_conn


router = APIRouter(tags=["outline-candidates"])


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _candidate_to_api(candidate: dict[str, Any]) -> dict[str, Any]:
    project = projects_dao.get_project(candidate["project_id"]) or {}
    project["chapters"] = chapters_dao.list_chapters(candidate["project_id"])
    removals = protected_saved_chapter_removals(
        project,
        candidate.get("candidate_outline_json") or {},
    )
    return {
        "id": candidate["id"],
        "projectId": candidate["project_id"],
        "runId": candidate.get("run_id") or "",
        "baseOutlineHash": candidate["base_outline_hash"],
        "baseOutlineVersion": int(candidate.get("base_outline_version") or 1),
        "candidateOutline": candidate.get("candidate_outline_json") or {},
        "previousOutline": candidate.get("previous_outline_json"),
        "diff": candidate.get("diff_json") or {},
        "instruction": candidate.get("instruction") or "",
        "status": candidate["status"],
        "createdAt": candidate.get("created_at") or "",
        "appliedAt": candidate.get("applied_at"),
        "requiresConfirmation": bool(removals),
        "protectedRemovals": removals,
    }


def _owned_candidate(request: Request, candidate_id: str) -> tuple[dict[str, Any], str]:
    candidate = candidates_dao.get_outline_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="大纲修改候选未找到。")
    user_id = require_project_owner(request, candidate["project_id"])
    if candidate["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问此大纲修改候选。")
    return candidate, user_id


@router.post("/projects/{project_id}/outline-candidates")
async def create_outline_candidate(project_id: str, data: OutlineCandidateCreate, request: Request):
    user_id = require_project_owner(request, project_id)
    project = projects_dao.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目未找到。")
    current_outline = project.get("outline") or {"mainSummary": "", "nodes": []}
    project_with_chapters = dict(project)
    project_with_chapters["chapters"] = chapters_dao.list_chapters(project_id)
    try:
        normalized = validate_outline_patch(
            project_with_chapters,
            data.candidateOutline,
            allow_saved_chapter_removal=True,
        )
    except OutlinePatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    base_version = max(1, int(current_outline.get("outlineVersion") or 1))
    normalized["outlineVersion"] = base_version + 1
    candidate = candidates_dao.create_outline_candidate(
        project_id=project_id,
        user_id=user_id,
        run_id=data.runId,
        base_outline_hash=outline_hash(current_outline),
        base_outline_version=base_version,
        candidate_outline_json=normalized,
        previous_outline_json=None,
        diff_json=build_outline_diff(current_outline, normalized),
        instruction=data.instruction,
        status="proposed",
    )
    return ApiResponse(data=_candidate_to_api(candidate))


@router.get("/projects/{project_id}/outline-candidates")
async def list_outline_candidates(
    project_id: str,
    request: Request,
    status: str | None = Query(default=None),
):
    user_id = require_project_owner(request, project_id)
    candidates = candidates_dao.list_outline_candidates(project_id, user_id, status=status)
    return ApiResponse(data=[_candidate_to_api(candidate) for candidate in candidates])


@router.post("/outline-candidates/{candidate_id}/apply")
async def apply_outline_candidate(
    candidate_id: str,
    request: Request,
    confirmSavedChapterRemoval: bool = Query(default=False),
):
    candidate, _ = _owned_candidate(request, candidate_id)
    if candidate["status"] != "proposed":
        raise HTTPException(status_code=409, detail="该大纲修改候选已处理。")

    now = _utc_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT outline FROM projects WHERE id = ?", (candidate["project_id"],)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="项目未找到。")
        current_outline = json.loads(row["outline"] or "null") or {"mainSummary": "", "nodes": []}
        if outline_hash(current_outline) != candidate["base_outline_hash"]:
            raise HTTPException(status_code=409, detail="大纲已发生变化，请基于最新大纲重新生成候选。")
        project = projects_dao.get_project(candidate["project_id"]) or {}
        project["outline"] = current_outline
        project["chapters"] = chapters_dao.list_chapters(candidate["project_id"])
        removals = protected_saved_chapter_removals(project, candidate["candidate_outline_json"])
        if removals and not confirmSavedChapterRemoval:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "saved_chapter_removal_confirmation_required",
                    "chapters": removals,
                    "message": "该候选会从大纲中移除已有正文的章节，需要用户明确确认。",
                },
            )
        result = conn.execute(
            """
            UPDATE outline_candidates
            SET status = 'applied', previous_outline_json = ?, updated_at = ?, applied_at = ?
            WHERE id = ? AND status = 'proposed'
            """,
            (json_dump(current_outline), now, now, candidate_id),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="该大纲修改候选已处理。")
        reconcile_outline_on_conn(conn, candidate["project_id"], current_outline, candidate["candidate_outline_json"])
        conn.execute(
            "UPDATE projects SET outline = ?, updated_at = ? WHERE id = ?",
            (json_dump(candidate["candidate_outline_json"]), now, candidate["project_id"]),
        )
    updated = candidates_dao.get_outline_candidate(candidate_id)
    return ApiResponse(data=_candidate_to_api(updated or candidate))


@router.post("/outline-candidates/{candidate_id}/reject")
async def reject_outline_candidate(candidate_id: str, request: Request):
    candidate, _ = _owned_candidate(request, candidate_id)
    if candidate["status"] != "proposed":
        raise HTTPException(status_code=409, detail="该大纲修改候选已处理。")
    updated = candidates_dao.update_outline_candidate_status(candidate_id, "rejected")
    return ApiResponse(data=_candidate_to_api(updated or candidate))


@router.post("/outline-candidates/{candidate_id}/revert")
async def revert_outline_candidate(candidate_id: str, request: Request):
    candidate, _ = _owned_candidate(request, candidate_id)
    if candidate["status"] != "applied" or not candidate.get("previous_outline_json"):
        raise HTTPException(status_code=409, detail="该候选当前不可撤销。")

    now = _utc_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute(
            """
            SELECT id FROM outline_candidates
            WHERE project_id = ? AND status = 'applied'
            ORDER BY applied_at DESC, id DESC LIMIT 1
            """,
            (candidate["project_id"],),
        ).fetchone()
        if latest is None or latest["id"] != candidate_id:
            raise HTTPException(status_code=409, detail="只能撤销最近一次已应用的大纲修改。")
        row = conn.execute("SELECT outline FROM projects WHERE id = ?", (candidate["project_id"],)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="项目未找到。")
        current_outline = json.loads(row["outline"] or "null") or {"mainSummary": "", "nodes": []}
        if outline_hash(current_outline) != outline_hash(candidate["candidate_outline_json"]):
            raise HTTPException(status_code=409, detail="大纲已再次变化，不能自动撤销。")
        reconcile_outline_on_conn(conn, candidate["project_id"], current_outline, candidate["previous_outline_json"])
        conn.execute(
            "UPDATE projects SET outline = ?, updated_at = ? WHERE id = ?",
            (json_dump(candidate["previous_outline_json"]), now, candidate["project_id"]),
        )
        conn.execute(
            "UPDATE outline_candidates SET status = 'reverted', updated_at = ? WHERE id = ?",
            (now, candidate_id),
        )
    updated = candidates_dao.get_outline_candidate(candidate_id)
    return ApiResponse(data=_candidate_to_api(updated or candidate))
