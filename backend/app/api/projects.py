from fastapi import APIRouter, HTTPException, Request
import time

from app.dao import projects as projects_dao
from app.dao import novel_facts as novel_facts_dao
from app.db import _sanitize_label, default_style_config, json_dump, utc_today
from app.models.schemas import (
    AnalyzeCharacterProfileRequest,
    ApiResponse,
    ProjectCreate,
    ProjectUpdate,
    gen_id,
)
from app.services.novel_generation_service import get_project_running_job, cancel_job_and_wait
from app.utils.text_repair import repair_data, repair_text


router = APIRouter(prefix="/projects", tags=["projects"])

# 简单 TTL 缓存：项目列表 5 秒内复用（P2 只读性能优化）
_list_cache: dict[str, tuple[float, list[dict]]] = {}

def _invalidate_list_cache() -> None:
    _list_cache.clear()


@router.get("/")
async def list_projects(request: Request):
    from app.core.auth import current_user_id, is_admin_user
    user_id = current_user_id(request)
    cache_key = user_id
    now = time.monotonic()
    if cache_key in _list_cache:
        cached_at, cached_data = _list_cache[cache_key]
        if now - cached_at < 5.0:
            return ApiResponse(data=cached_data)
    data = projects_dao.list_projects(None if is_admin_user(user_id) else user_id)
    _list_cache[cache_key] = (now, data)
    return ApiResponse(data=data)


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)
    project = projects_dao.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目未找到。")
    return ApiResponse(data=project)


@router.post("/")
async def create_project(data: ProjectCreate, request: Request):
    from app.core.auth import current_user_id
    user_id = current_user_id(request)
    today = utc_today()
    protagonist_name = repair_text(data.protagonistName).strip()
    project = {
        "id": gen_id(),
        # ISSUE-001：空字符串/纯空白名称兜底为"未命名项目"（与 db.row_to_project 读取逻辑一致）
        "name": _sanitize_label(repair_text(data.name), "未命名项目"),
        "subtitle": repair_text(data.subtitle),
        "genre": data.genre,
        "targetWordCount": data.targetWordCount,
        "currentWordCount": 0,
        "status": "planning",
        "createdAt": today,
        "updatedAt": today,
        "volumes": [],
        "characters": ([{
            "id": gen_id(), "name": protagonist_name, "role": "protagonist",
            "basicInfo": {"identity": "主角"},
        }] if protagonist_name else []),
        "locations": [],
        "worldRules": [],
        "inspirationFragments": [],
        "styleConfig": default_style_config(data.stylePreset),
        "outline": None,
        "tags": [],
        "isStarred": False,
        "premise": repair_text(data.premise),
        "targetChapters": data.targetChapters,
        "importAnalysis": None,
    }

    projects_dao.create_project({
        "id": project["id"],
        "owner_id": user_id,
        "name": project["name"],
        "subtitle": project["subtitle"],
        "genre": project["genre"],
        "target_word_count": project["targetWordCount"],
        "current_word_count": project["currentWordCount"],
        "status": project["status"],
        "created_at": project["createdAt"],
        "updated_at": project["updatedAt"],
        "style_config": json_dump(project["styleConfig"]),
        "outline": json_dump(project["outline"]),
        "tags": json_dump(project["tags"]),
        "is_starred": int(project["isStarred"]),
        "premise": project["premise"],
        "target_chapters": project["targetChapters"],
        "characters": json_dump(project["characters"]),
        "locations": "[]",
        "world_rules": "[]",
        "import_analysis": "null",
        "inspiration_fragments": "[]",
    })
    _invalidate_list_cache()
    return ApiResponse(data=project)


@router.patch("/{project_id}")
async def update_project(project_id: str, data: ProjectUpdate, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)
    updates = data.model_dump(exclude_none=True)
    if not updates:
        return await get_project(project_id, request)

    allowed_columns = {
        "name": "name",
        "subtitle": "subtitle",
        "status": "status",
        "isStarred": "is_starred",
        "currentWordCount": "current_word_count",
        "outline": "outline",
        "characters": "characters",
        "locations": "locations",
        "worldRules": "world_rules",
        "importAnalysis": "import_analysis",
        "inspirationFragments": "inspiration_fragments",
    }

    field_updates: dict[str, object] = {}
    for key, value in updates.items():
        column = allowed_columns.get(key)
        if not column:
            continue
        if key == "isStarred":
            field_updates[column] = int(value)
        elif key == "name":
            # ISSUE-001：更新名称时同样兜底空字符串/纯空白
            field_updates[column] = _sanitize_label(repair_text(value), "未命名项目")
        elif key in {"outline", "characters", "locations", "worldRules", "importAnalysis", "inspirationFragments"}:
            repaired = repair_data(value)
            # 兼容已序列化的 JSON 字符串——避免 json_dump 二次转义
            field_updates[column] = repaired if isinstance(repaired, str) else json_dump(repaired)
        else:
            field_updates[column] = repair_text(value)

    if not field_updates:
        return await get_project(project_id, request)

    rowcount = projects_dao.update_project_fields(project_id, field_updates, utc_today())
    if rowcount == 0:
        raise HTTPException(status_code=404, detail="项目未找到。")

    project = projects_dao.get_project(project_id)
    _invalidate_list_cache()
    _invalidate_list_cache()
    return ApiResponse(data=project)


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request):
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)
    """删除项目。如有进行中的整书创作任务，先安全取消再删除（Codex P1：消除 409 竞态）。"""
    running_job = get_project_running_job(project_id)
    if running_job:
        # cancel + 轮询等待终态（超时 10 秒后强制标记，不阻塞删除）
        cancel_job_and_wait(running_job.id, timeout_seconds=10)

    rowcount = projects_dao.delete_project(project_id)
    if rowcount == 0:
        raise HTTPException(status_code=404, detail="项目未找到。")
    _invalidate_list_cache()
    return ApiResponse(data=None)


@router.post("/{project_id}/analyze-characters")
async def analyze_characters(project_id: str, request: Request):
    """一键分析项目正文中的人物与人物关系（读取全部章节，AI 分析后返回）。"""
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)

    from app.services.character_analysis_service import analyze_project_characters

    result = await analyze_project_characters(project_id)
    return ApiResponse(data=result)


@router.post("/{project_id}/analyze-character-profile")
async def analyze_character_profile(project_id: str, payload: AnalyzeCharacterProfileRequest, request: Request):
    """通读章节正文与大纲，为指定角色生成各档案字段的建议值（只产出建议，由作者逐项决定是否采用）。

    AI 不可用或返回内容不合法时返回 200 + `degraded=true`，`suggestions` 为空，
    由前端就地显示原因；绝不会用规则引擎编造角色资料。
    """
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)

    from app.services.character_profile_service import (
        CharacterNotFoundError,
        analyze_character_profile,
    )

    character_name = payload.characterName.strip()
    if not character_name:
        raise HTTPException(status_code=422, detail="角色名不能为空。")

    try:
        result = await analyze_character_profile(project_id, character_name)
    except CharacterNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse(data=result)


# ═══════════════════════════════════════════════════════════
# P2-2b 写作冲突（novel_facts is_conflict=1）—— 任务中心冲突视图
# ═══════════════════════════════════════════════════════════

def _parse_conflict_content(content: str) -> tuple[str, str]:
    """拆分落盘冲突文本 `"{summary}（根因：{root_cause}）"` 为 (summary, root_cause)。"""
    text = content or ""
    marker = "（根因："
    if marker in text:
        summary, _, rest = text.partition(marker)
        root_cause = rest.rstrip("）").strip()
        return summary.strip(), root_cause
    return text.strip(), ""


def _conflict_to_api(row: dict) -> dict:
    summary, root_cause = _parse_conflict_content(row.get("content") or "")
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "content": row.get("content") or "",
        "summary": summary,
        "rootCause": root_cause,
        "chapterId": row.get("chapter_id") or "",
        "createdAt": row.get("created_at") or "",
    }


@router.get("/{project_id}/conflicts")
async def list_writing_conflicts(project_id: str, request: Request):
    """P2-2b：查询项目全部写作冲突（is_conflict=1，新→旧），供任务中心冲突视图。

    字段：id / projectId / content（完整落盘文本）/ summary（内容摘要）/
          rootCause（根因，已从 content 拆分）/ chapterId / createdAt。
    """
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)
    rows = novel_facts_dao.list_writing_conflicts(project_id)
    return ApiResponse(data=[_conflict_to_api(row) for row in rows])


@router.patch("/{project_id}/conflicts/{fact_id}")
async def resolve_writing_conflict(project_id: str, fact_id: str, request: Request):
    """P2-2b：标记写作冲突「已处理」——删除该条 is_conflict=1 记录。

    冲突经人工决策后不再展示，也不重新写入账本（与正常事实隔离）。
    """
    from app.core.auth import require_project_owner
    require_project_owner(request, project_id)
    ok = novel_facts_dao.delete_writing_conflict(project_id, fact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="写作冲突未找到或已处理。")
    return ApiResponse(data=None)
