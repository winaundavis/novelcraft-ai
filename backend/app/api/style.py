"""M4 文风画像引擎 · API 路由。

端点（前缀 /api/projects）：
- POST /{project_id}/style-analysis   触发深度文风分析（同步执行，LLM 失败自动降级）
- GET  /{project_id}/style-profile    查询画像 + 报告
- PUT  /{project_id}/style-transition 设置过渡档位（快/中/慢）
- GET  /{project_id}/style-transition 查询过渡进度
"""

from fastapi import APIRouter, HTTPException, Request
from app.core.auth import require_project_owner
from pydantic import BaseModel, Field, StrictInt

from app.dao import style_dao
from app.models.schemas import ApiResponse
from app.services.style_engine import profile_service
from app.services.style_engine import transition_service


router = APIRouter(prefix="/projects", tags=["style"])


class StyleTransitionTarget(BaseModel):
    presetKey: str = ""
    customPrompt: str = Field(default="", max_length=2000)


class StyleTransitionRequest(BaseModel):
    pace: str = "medium"
    targetChapters: StrictInt | None = Field(default=None, ge=1, le=200)
    targetStyle: StyleTransitionTarget | None = None


class GenreStyleProfileRequest(BaseModel):
    genre: str
    name: str = ""


class DuplicateStyleProfileRequest(BaseModel):
    name: str = ""


@router.post("/{project_id}/style-analysis")
async def analyze_project_style(project_id: str, request: Request):
    """触发一次深度文风分析：采样 → LLM 抽取四组规则 → 落库画像 + 绑定。

    项目无正文返回 400；LLM 未配置/调用失败时自动降级为规则统计（status='degraded'）。
    """
    require_project_owner(request, project_id)
    try:
        profile = await profile_service.analyze_project_style(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=profile)


@router.get("/{project_id}/style-profile")
async def get_project_style_profile(project_id: str, request: Request):
    """查询项目当前生效的文风画像与报告。"""
    require_project_owner(request, project_id)
    profile = profile_service.get_profile(project_id)
    if not profile:
        raise HTTPException(status_code=404, detail="项目尚未进行深度文风分析。")
    return ApiResponse(data=profile)


@router.get("/{project_id}/style-profiles")
async def list_project_style_profiles(project_id: str, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=style_dao.list_profiles(project_id))


@router.post("/{project_id}/genre-style-profile")
async def create_genre_style_profile(project_id: str, data: GenreStyleProfileRequest, request: Request):
    require_project_owner(request, project_id)
    return ApiResponse(data=profile_service.create_genre_style_profile(project_id, data.genre, data.name))


@router.post("/{project_id}/style-profiles/{profile_id}/duplicate")
async def duplicate_style_profile(project_id: str, profile_id: str, data: DuplicateStyleProfileRequest, request: Request):
    require_project_owner(request, project_id)
    source = style_dao.get_profile(profile_id)
    if not source or source.get("projectId") != project_id:
        raise HTTPException(status_code=404, detail="文风画像未找到。")
    profile = style_dao.duplicate_profile(profile_id, name=data.name)
    return ApiResponse(data=profile)


@router.put("/{project_id}/style-profiles/{profile_id}/activate")
async def activate_style_profile(project_id: str, profile_id: str, request: Request):
    require_project_owner(request, project_id)
    profile = style_dao.get_profile(profile_id)
    if not profile or profile.get("projectId") != project_id:
        raise HTTPException(status_code=404, detail="文风画像未找到。")
    style_dao.upsert_binding(project_id, profile_id, profile.get("updatedAt") or "")
    return ApiResponse(data=profile)


@router.put("/{project_id}/style-transition")
async def set_project_style_transition(project_id: str, data: StyleTransitionRequest, request: Request):
    """设置渐进过渡档位（fast/medium/slow）并生成过渡计划。"""
    require_project_owner(request, project_id)
    if data.pace not in transition_service.VALID_PACES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的过渡档位：{data.pace}（可选：{'/'.join(sorted(transition_service.VALID_PACES))}）",
        )
    try:
        transition = transition_service.start_transition(
            project_id,
            data.pace,
            target_chapters=data.targetChapters,
            target_style=data.targetStyle.model_dump() if data.targetStyle else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=transition)


@router.get("/{project_id}/style-transition")
async def get_project_style_transition(project_id: str, request: Request):
    """查询项目当前的过渡进度（含当前档位与微调指令）。"""
    require_project_owner(request, project_id)
    transition = transition_service.get_transition_progress(project_id)
    if not transition:
        raise HTTPException(status_code=404, detail="项目尚未设置风格过渡。")
    return ApiResponse(data=transition)


@router.get("/{project_id}/style-contract")
async def get_project_style_contract(project_id: str, request: Request):
    """编译项目当前生效的风格契约，返回可直接注入续写请求的 styleTone（P1-3）。

    契约含：预设五维表达偏好（含 ±20% 允许区间）+ 画像规则（软参考）+ 过渡档位微调；
    风格偏好不得覆盖故事事实。
    编辑器逐章续写时拉取此端点，替代原来只有一行的 styleTone。
    """
    require_project_owner(request, project_id)
    contract = profile_service.build_generation_contract(project_id)
    prompt_block = profile_service.contract_to_prompt_block(contract)
    return ApiResponse(
        data={
            "styleTone": prompt_block,
            "contract": contract,
            **transition_service.plan_token(contract),
        }
    )
