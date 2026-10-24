"""P2 伏笔账本 · API 端点。

六端点：
- GET    /api/projects/{project_id}/foreshadows                列表（含一致性报告 / 超期计数）
- POST   /api/projects/{project_id}/foreshadows/suggest        建议候选（LLM 失败降级空列表）
- POST   /api/projects/{project_id}/foreshadows/draft          作者手写文字 → 伏笔草稿（不落库）
- POST   /api/projects/{project_id}/foreshadows                确认入库（批量，status=planted）
- PATCH  /api/projects/{project_id}/foreshadows/{fid}          手动改状态/描述/回收方式（可附备注）
- DELETE /api/projects/{project_id}/foreshadows/{fid}          删除

前端展示字段统一 camelCase（与 projects/chapters/style 一致）；DAO 层为 snake_case。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel

from app.dao import foreshadows as foreshadows_dao
from app.models.schemas import ApiResponse
from app.services import foreshadow_service


router = APIRouter(prefix="/projects/{project_id}/foreshadows", tags=["foreshadows"])


class ForeshadowItemIn(BaseModel):
    name: str
    description: str = ""
    plantChapterId: str = ""
    targetChapterId: str = ""


class ForeshadowCreateRequest(BaseModel):
    items: list[ForeshadowItemIn]


class ForeshadowDraftRequest(BaseModel):
    text: str
    plantChapterId: str = ""


class ForeshadowUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    resolution: Optional[str] = None
    # 手动登记：事件说明 + 关联章节（用于溯源「作者在哪一章自己推进/回收了它」）
    note: str = ""
    chapterId: str = ""


def _to_api(item: dict[str, Any]) -> dict[str, Any]:
    """snake_case（DAO/服务）→ camelCase（前端）。"""
    return {
        "id": item["id"],
        "projectId": item["project_id"],
        "name": item["name"],
        "description": item["description"],
        "plantChapterId": item["plant_chapter_id"],
        "targetChapterId": item["target_chapter_id"],
        "status": item["status"],
        "resolution": item["resolution"],
        "events": item.get("events") or [],
        "plantChapter": item.get("plant_chapter") or 0,
        "targetChapter": item.get("target_chapter") or 0,
        "totalChapters": item.get("total_chapters") or 0,
        "overdue": bool(item.get("overdue")),
        "overdueBy": item.get("overdue_by") or 0,
        "createdAt": item["created_at"],
        "updatedAt": item["updated_at"],
    }


@router.get("/")
async def list_foreshadows(project_id: str, request: Request):
    """伏笔列表（含一致性报告与超期计数，供前端伏笔管理面板展示）。"""
    require_project_owner(request, project_id)
    outline = foreshadow_service._load_outline(project_id)
    items = foreshadow_service.enrich_foreshadows(project_id, outline)
    consistency = foreshadow_service.check_foreshadow_outline_consistency(project_id, outline)
    alert_count = sum(1 for item in items if item.get("overdue"))
    return ApiResponse(
        data={
            "items": [_to_api(item) for item in items],
            "consistency": consistency,
            "alertCount": alert_count,
        }
    )


@router.post("/suggest")
async def suggest_foreshadows(project_id: str, request: Request):
    """AI 建议伏笔候选（基于全书大纲）；LLM 失败降级返回空列表，不报错。"""
    require_project_owner(request, project_id)
    candidates = await foreshadow_service.suggest_foreshadows(project_id)
    return ApiResponse(data=candidates)


@router.post("/draft")
async def draft_foreshadows(project_id: str, req: ForeshadowDraftRequest, request: Request):
    """把作者手写的文字整理成伏笔草稿（不落库；AI 不可用时 degraded + 中文原因）。"""
    require_project_owner(request, project_id)
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="请先写下你要登记的伏笔内容。")
    result = await foreshadow_service.draft_foreshadows_from_text(
        project_id, req.text, req.plantChapterId
    )
    return ApiResponse(data=result)


@router.post("/")
async def create_foreshadows(project_id: str, req: ForeshadowCreateRequest, request: Request):
    """用户确认建议后批量入库（status=planted）。"""
    require_project_owner(request, project_id)
    if not req.items:
        raise HTTPException(status_code=400, detail="至少需要一条伏笔。")
    items = [
        {
            "name": item.name,
            "description": item.description,
            "plant_chapter_id": item.plantChapterId,
            "target_chapter_id": item.targetChapterId,
        }
        for item in req.items
    ]
    created_ids = foreshadows_dao.create_foreshadows_batch(project_id, items)
    outline = foreshadow_service._load_outline(project_id)
    all_items = foreshadow_service.enrich_foreshadows(project_id, outline)
    created = [_to_api(item) for item in all_items if item["id"] in created_ids]
    return ApiResponse(data=created)


@router.patch("/{fid}")
async def update_foreshadow(project_id: str, fid: str, req: ForeshadowUpdateRequest, request: Request):
    """手动修改伏笔（状态/描述/回收方式）；状态变更自动记录事件。"""
    require_project_owner(request, project_id)
    fields: dict[str, Any] = {}
    if req.name is not None:
        fields["name"] = req.name
    if req.description is not None:
        fields["description"] = req.description
    if req.status is not None:
        fields["status"] = req.status
    if req.resolution is not None:
        fields["resolution"] = req.resolution
    chapter_order = 0
    if req.chapterId:
        outline = foreshadow_service._load_outline(project_id)
        order_map = foreshadow_service._order_map(
            foreshadow_service.chapter_nodes_from_outline(outline)
        )
        chapter_order = order_map.get(req.chapterId, 0)
    ok = foreshadows_dao.update_foreshadow(
        project_id,
        fid,
        **fields,
        event_note=req.note,
        chapter_id=req.chapterId,
        chapter_order=chapter_order,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="伏笔未找到或状态迁移非法。")
    outline = foreshadow_service._load_outline(project_id)
    item = next(
        (i for i in foreshadow_service.enrich_foreshadows(project_id, outline) if i["id"] == fid),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail="伏笔未找到。")
    return ApiResponse(data=_to_api(item))


@router.delete("/{fid}")
async def delete_foreshadow(project_id: str, fid: str, request: Request):
    require_project_owner(request, project_id)
    ok = foreshadows_dao.delete_foreshadow(project_id, fid)
    if not ok:
        raise HTTPException(status_code=404, detail="伏笔未找到。")
    return ApiResponse(data=None)
