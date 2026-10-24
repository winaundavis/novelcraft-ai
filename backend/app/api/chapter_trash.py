"""Chapter recycle-bin access always requires project ownership."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.auth import require_project_owner
from app.models.schemas import ApiResponse
from app.services import chapter_trash_service as trash

router = APIRouter(prefix="/projects/{project_id}/chapter-trash", tags=["chapter-trash"])


class RestoreChapter(BaseModel):
    targetChapterId: str = Field(min_length=1, max_length=200)
    outlineHash: str = Field(max_length=128)
    targetContentHash: str = Field(max_length=128)
    confirmReplace: bool = False


class BatchRestoreChapters(BaseModel):
    """批量还原：只回各自的原章节，因此不需要逐条指定目标章节。"""
    ids: list[str] = Field(min_length=1, max_length=500)
    outlineHash: str = Field(max_length=128)


class BatchDeleteChapters(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


@router.get("")
async def list_recycled_chapters(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=trash.list_trash(project_id))


@router.get("/{trash_id}")
async def read_recycled_chapter(project_id: str, trash_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=trash.get_trash(project_id, trash_id))


@router.post("/{trash_id}/restore")
async def restore_recycled_chapter(project_id: str, trash_id: str, data: RestoreChapter, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=trash.restore_chapter(
        project_id, trash_id, target_chapter_id=data.targetChapterId,
        expected_outline_hash=data.outlineHash, target_content_hash=data.targetContentHash,
        confirm_replace=data.confirmReplace,
    ))


@router.delete("/{trash_id}")
async def delete_recycled_chapter(project_id: str, trash_id: str, request: Request):
    require_project_owner(request, project_id)
    trash.permanently_delete(project_id, trash_id)
    return ApiResponse(data=None)

# 批量路由放在单条路由之后：路径形状不冲突（`/{id}/restore` 是两段，`/batch-restore` 是一段），
# 但保持「先单条后批量」的阅读顺序。两个批量接口都返回逐条结果，而不是一个笼统的成功/失败。
@router.post("/batch-restore")
async def batch_restore_recycled_chapters(project_id: str, data: BatchRestoreChapters, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=trash.batch_restore_chapters(project_id, data.ids, data.outlineHash))


@router.post("/batch-delete")
async def batch_delete_recycled_chapters(project_id: str, data: BatchDeleteChapters, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=trash.permanently_delete_many(project_id, data.ids))
