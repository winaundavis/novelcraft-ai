from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner

from app.dao import chapter_commits as chapter_commits_dao
from app.dao import chapters as chapters_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import projects as projects_dao
from app.db import get_conn, row_to_chapter, utc_today
from app.models.schemas import ApiResponse, ChapterCreate, ChapterSave, gen_id
from app.services import novel_generation_service as ngs
from app.services import chapter_digest_service, projection_runner_service
from app.services.style_engine import transition_service
from app.utils.text_repair import repair_text
from app.utils.word_count import count_words


router = APIRouter(prefix="/projects/{project_id}/chapters", tags=["chapters"])


@router.get("/")
async def list_chapters(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=chapters_dao.list_chapters(project_id))


@router.get("/{chapter_id}")
async def get_chapter(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    chapter = chapters_dao.get_chapter(chapter_id, project_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节未找到。")
    return ApiResponse(data=chapter)


@router.get("/{chapter_id}/projections")
async def list_chapter_projections(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    """P1：查询章节的 5 路记忆投影状态（chapter_summary/facts/entities/volume/fts）。

    供前端展示「正文已保存但记忆未同步」的可见状态与单项重试入口。
    """
    if not chapters_dao.get_chapter(chapter_id, project_id):
        raise HTTPException(status_code=404, detail="章节未找到。")
    rows = chapter_commits_dao.list_projections(project_id, chapter_id)
    return ApiResponse(data=rows)


@router.get("/{chapter_id}/canonical-commit")
async def get_canonical_commit(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    if not chapters_dao.get_chapter(chapter_id, project_id):
        raise HTTPException(status_code=404, detail="chapter not found")
    return ApiResponse(data=chapter_commits_dao.get_canonical_commit(project_id, chapter_id))


@router.get("/{chapter_id}/digest")
async def get_chapter_digest(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    if not chapters_dao.get_chapter(chapter_id, project_id):
        raise HTTPException(status_code=404, detail="chapter not found")
    return ApiResponse(data=chapter_digest_service.get_digest(project_id, chapter_id))


@router.post("/replay")
async def replay_project_projections(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=projection_runner_service.replay_project(project_id))


@router.post("/{chapter_id}/projections/{projection_type}/replay")
async def replay_chapter_projection(project_id: str, chapter_id: str, projection_type: str, request: Request):
    require_project_owner(request, project_id)
    """P1：重放单项记忆投影（某项 failed/skipped 只重跑该项，幂等）。

    重复点击安全：重放基于章节当前正文与 DB 状态，不产生重复副作用。
    """
    if projection_type not in (*chapter_commits_dao.PROJECTION_TYPES, *projection_runner_service.WRITERS):
        raise HTTPException(status_code=400, detail=f"未知投影类型：{projection_type}")
    if not chapters_dao.get_chapter(chapter_id, project_id):
        raise HTTPException(status_code=404, detail="章节未找到。")
    try:
        result = await ngs.replay_memory_projection(
            project_id=project_id,
            chapter_id=chapter_id,
            projection_type=projection_type,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ApiResponse(data=result)


@router.post("/")
async def create_chapter(project_id: str, data: ChapterCreate, request: Request):
    require_project_owner(request, project_id)
    today = utc_today()
    chapter = {
        "id": gen_id(),
        "projectId": project_id,
        "volumeId": data.volumeId,
        "title": repair_text(data.title),
        "order": data.order,
        "content": "",
        "wordCount": 0,
        "status": "draft",
        "summary": "",
        "notes": "",
        "createdAt": today,
        "updatedAt": today,
    }

    chapters_dao.insert_chapter({
        "id": chapter["id"],
        "project_id": chapter["projectId"],
        "volume_id": chapter["volumeId"],
        "title": chapter["title"],
        "chapter_order": chapter["order"],
        "content": chapter["content"],
        "word_count": chapter["wordCount"],
        "status": chapter["status"],
        "summary": chapter["summary"],
        "notes": chapter["notes"],
        "created_at": chapter["createdAt"],
        "updated_at": chapter["updatedAt"],
    })
    return ApiResponse(data=chapter)


@router.put("/{chapter_id}")
async def save_chapter(project_id: str, chapter_id: str, data: ChapterSave, request: Request):
    require_project_owner(request, project_id)
    today = utc_today()
    content = repair_text(data.content or "")
    word_count = count_words(content)
    title = repair_text(data.title) if data.title else None
    volume_id = data.volumeId
    order = data.order

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        ).fetchone()
        try:
            transition_service.validate_plan_on_conn(
                conn, project_id, data.stylePlanId, data.stylePlanRevision,
                chapter_id=chapter_id,
                allow_completed_revision=existing is not None and existing["content"] == content,
            )
        except transition_service.TransitionRevisionConflict as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc

        if existing is None:
            chapters_dao._insert_chapter_on_conn(conn, {
                "id": chapter_id,
                "project_id": project_id,
                "volume_id": volume_id or "",
                "title": title or "Untitled Chapter",
                "chapter_order": order or 1,
                "content": content,
                "word_count": word_count,
                "status": "writing",
                "summary": "",
                "notes": "",
                "created_at": today,
                "updated_at": today,
            })
        else:
            chapters_dao._update_chapter_on_conn(conn, chapter_id, project_id, {
                "volume_id": volume_id if volume_id is not None else existing["volume_id"],
                "title": title if title is not None else existing["title"],
                "chapter_order": order if order is not None else existing["chapter_order"],
                "content": content,
                "word_count": word_count,
                "status": "completed" if word_count > 0 else existing["status"],
                "updated_at": today,
            })

        projects_dao._update_project_word_count_on_conn(conn, project_id, today)
        if word_count > 0:
            try:
                transition_service.record_chapter_completion_on_conn(
                    conn,
                    project_id,
                    chapter_id,
                    data.stylePlanId,
                    data.stylePlanRevision,
                    source="manual_save",
                )
            except transition_service.TransitionRevisionConflict as exc:
                raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
        row = conn.execute(
            "SELECT * FROM chapters WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        ).fetchone()

    # 记忆升级：保存章节后同步 FTS 索引（含正文/摘要，保证可检索）。
    # 失败降级（FTS 不可用 / 表缺失时静默跳过），绝不阻塞保存。
    try:
        memory_fts_dao.sync_chapter_fts(project_id, chapter_id)
    except Exception:  # noqa: BLE001
        pass

    return ApiResponse(data={**row_to_chapter(row), "styleTransition": transition_service.get_transition_progress(project_id)})


@router.delete("/{chapter_id}")
async def delete_chapter(project_id: str, chapter_id: str, request: Request):
    require_project_owner(request, project_id)
    from app.services.chapter_trash_service import recycle_chapter
    recycle_chapter(project_id, chapter_id)
    return ApiResponse(data=None)
