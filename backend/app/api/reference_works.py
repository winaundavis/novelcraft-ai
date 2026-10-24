"""P2-3 参考书档案 · API 端点（前缀 /api/projects）。

档案是**用户级个人资产**（跨项目共享）：路径里的 project_id 只是权限入口与「来源项目」标注，
列表返回该作者的**全部**档案（带来源项目名）；绑定（正在参考哪一份）仍是**项目级**。

- POST   /{project_id}/reference-works                     建档（拆解 + 落库，永不改文风绑定）
- GET    /{project_id}/reference-works                     列表（该作者全部档案；带 activeReferenceId）
- GET    /{project_id}/reference-works/{reference_id}      单条（含原文，供回看）
- PATCH  /{project_id}/reference-works/{reference_id}      改名 / 改来源备注
- DELETE /{project_id}/reference-works/{reference_id}      删除（同时清理各项目里指向它的启用绑定）
- POST   /{project_id}/reference-works/{reference_id}/reanalyze  用已存原文重新拆解
- PUT    /{project_id}/reference-works/active             显式启用/停用「创作参考」（默认不启用）

产品约定：参考档案是「别人的作品」的写法档案，**只读参考、不被自动套用**；
与文风档案（style_profiles）刻意分表，绝不进 style_bindings。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.auth import require_project_owner
from app.dao import reference_works as reference_dao
from app.models.schemas import ApiResponse
from app.services import reference_work_service


router = APIRouter(prefix="/projects", tags=["reference-works"])


class ReferenceWorkCreateRequest(BaseModel):
    name: str = ""
    text: str = Field(default="")
    sourceNote: str = ""
    tags: list[str] | str | None = None


class ReferenceWorkUpdateRequest(BaseModel):
    name: str | None = None
    sourceNote: str | None = None
    # 标签：可以传数组，也可以传「逗号/顿号分隔」的字符串（服务端会规范化）
    tags: list[str] | str | None = None


class ReferenceActiveItem(BaseModel):
    referenceId: str = ""
    weight: int = 6   # 1-10，越大越优先（预算紧张时先保留高强度的）


class ReferenceActiveRequest(BaseModel):
    """**整体替换**本项目的启用集合；传空数组即全部停用。

    兼容单份写法：只给 `referenceId` 时等价于「只启用这一份」。
    """
    items: list[ReferenceActiveItem] | None = None
    referenceId: str = ""
    weight: int = 6


@router.post("/{project_id}/reference-works")
async def create_reference_work(project_id: str, req: ReferenceWorkCreateRequest, request: Request):
    """粘贴/上传一段参考文本 → 建档（本地算指标 + 至多一次 AI 抽规则；失败降级不报错）。"""
    owner_id = require_project_owner(request, project_id)
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请先粘贴或上传参考文本。")
    if len(text) > reference_work_service.MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"参考文本过长（{len(text)} 字），上限 {reference_work_service.MAX_INPUT_CHARS} 字，请拆分后再试。",
        )
    record = await reference_work_service.create_reference_work(
        project_id, owner_id, text, name=req.name, source_note=req.sourceNote, tags=req.tags
    )
    return ApiResponse(data=record)


@router.get("/{project_id}/reference-works")
async def list_reference_works(project_id: str, request: Request):
    """**该作者的全部**参考档案（跨项目共享，不含原文）+ 本项目启用为创作参考的那些。"""
    owner_id = require_project_owner(request, project_id)
    active = reference_work_service.get_active_references(project_id)
    return ApiResponse(
        data={
            "items": reference_dao.list_reference_works(owner_id),
            "activeReferences": [
                {"id": str(item.get("id") or ""), "weight": int(item.get("weight") or 6), "name": str(item.get("name") or "")}
                for item in active
            ],
            # 兼容旧字段：强度最高的那份（没有启用则为空串）
            "activeReferenceId": str((active[0] if active else {}).get("id") or ""),
        }
    )


@router.put("/{project_id}/reference-works/active")
async def set_active_reference_work(project_id: str, req: ReferenceActiveRequest, request: Request):
    """整体替换「创作参考」集合（可多份 + 强度）。默认不启用；只影响表达参考，不改本书文风画像。"""
    owner_id = require_project_owner(request, project_id)
    if req.items is not None:
        pairs = [(item.referenceId, item.weight) for item in req.items if item.referenceId]
    else:
        pairs = [(req.referenceId, req.weight)] if req.referenceId else []
    active = reference_work_service.set_active_references(project_id, owner_id, pairs)
    if active is None:
        raise HTTPException(status_code=404, detail="参考档案未找到。")
    return ApiResponse(
        data={
            "activeReferences": [
                {"id": str(item.get("id") or ""), "weight": int(item.get("weight") or 6), "name": str(item.get("name") or "")}
                for item in active
            ],
            "activeReferenceId": str((active[0] if active else {}).get("id") or ""),
        }
    )


@router.get("/{project_id}/reference-works/{reference_id}")
async def get_reference_work(project_id: str, reference_id: str, request: Request):
    owner_id = require_project_owner(request, project_id)
    record = reference_dao.get_reference_work(owner_id, reference_id)
    if not record:
        raise HTTPException(status_code=404, detail="参考档案未找到。")
    return ApiResponse(data=record)


@router.patch("/{project_id}/reference-works/{reference_id}")
async def update_reference_work(project_id: str, reference_id: str, req: ReferenceWorkUpdateRequest, request: Request):
    owner_id = require_project_owner(request, project_id)
    record = reference_dao.update_reference_work(
        owner_id, reference_id, name=req.name, source_note=req.sourceNote, tags=req.tags
    )
    if not record:
        raise HTTPException(status_code=404, detail="参考档案未找到。")
    return ApiResponse(data=record)


@router.delete("/{project_id}/reference-works/{reference_id}")
async def delete_reference_work(project_id: str, reference_id: str, request: Request):
    owner_id = require_project_owner(request, project_id)
    if not reference_dao.delete_reference_work(owner_id, reference_id):
        raise HTTPException(status_code=404, detail="参考档案未找到。")
    return ApiResponse(data=None)


@router.post("/{project_id}/reference-works/{reference_id}/reanalyze")
async def reanalyze_reference_work(project_id: str, reference_id: str, request: Request):
    """用已存原文重新拆解（上次 AI 不可用时可用它重试；名称与备注保留）。"""
    owner_id = require_project_owner(request, project_id)
    try:
        record = await reference_work_service.reanalyze_reference_work(owner_id, reference_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not record:
        raise HTTPException(status_code=404, detail="参考档案未找到。")
    return ApiResponse(data=record)
