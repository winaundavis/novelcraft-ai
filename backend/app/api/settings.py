"""Settings APIs for runtime LLM configuration."""

from fastapi import APIRouter, Depends, HTTPException
from app.core.auth import require_maintainer
from pydantic import BaseModel, Field

from app.dao import llm_configs as llm_dao
from app.llm.provider import llm
from app.llm.roles import ALL_ROLES, DEFAULT_ROLE_PROFILES, ROLE_DESCRIPTIONS, ROLE_LABELS
from app.models.schemas import ApiResponse, gen_id
from app.db import utc_today, json_dump
from app.core.secret_box import SecretBoxError, encrypt_secret


router = APIRouter(prefix="/settings", tags=["settings"])


class LLMConfigCreate(BaseModel):
    provider: str
    apiKey: str = Field(min_length=1)
    apiBase: str = ""
    model: str = Field(min_length=1)
    temperature: float = 0.8
    maxTokens: int = 4096
    # 建议4 模型档位路由：角色级模型覆盖（如 {"check": {"provider": "deepseek", "model": "deepseek-flash"}}）
    roleOverrides: dict = {}
    isActive: bool = True


class RoleOverridesUpdate(BaseModel):
    """仅更新指定配置的角色级模型覆盖（建议1 写作档位前端配套）。"""

    roleOverrides: dict = {}


def _normalize_role_overrides(overrides: dict) -> dict:
    """清洗角色覆盖：只保留合法角色与合法字段，防御非法输入。

    第 2 档：支持 api_key / api_base（独立 key/base）与 long_model（长度路由长窗模型）。
    api_key 为字符串时原样保留（含空串表示清除、含 `*` 表示掩码保留，由端点解释）。
    """
    normalized: dict = {}
    if not isinstance(overrides, dict):
        return normalized
    for role, cfg in overrides.items():
        if role not in ALL_ROLES or not isinstance(cfg, dict):
            continue
        cleaned: dict = {}
        provider = str(cfg.get("provider") or "").strip()
        model = str(cfg.get("model") or "").strip()
        temperature = cfg.get("temperature")
        thinking = str(cfg.get("thinking") or "").strip().lower()
        api_key = cfg.get("api_key")
        if api_key is None:
            api_key = cfg.get("apiKey")
        api_base = str(cfg.get("api_base") or cfg.get("apiBase") or "").strip()
        long_model = str(cfg.get("long_model") or cfg.get("longModel") or "").strip()
        if provider:
            cleaned["provider"] = provider
        if model:
            cleaned["model"] = model
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            cleaned["temperature"] = round(float(temperature), 2)
        if thinking in {"enabled", "disabled"}:
            cleaned["thinking"] = thinking
        if isinstance(api_key, str):
            cleaned["api_key"] = api_key.strip()  # 含空串（清除）与掩码（保留），端点解释
        if api_base:
            cleaned["api_base"] = api_base
        if long_model:
            cleaned["long_model"] = long_model
        if cleaned:
            normalized[role] = cleaned
    return normalized


def _is_masked_api_key(value: str) -> bool:
    """掩码值检测：脱敏后的 key 必然含 `*`，真实 key 不含。"""
    return bool(value and "*" in value)


def _mask_api_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _mask_role_overrides(overrides: dict) -> dict:
    """对 role_overrides 内各角色的 api_key 脱敏（复制，不修改原数据）。"""
    masked: dict = {}
    if not isinstance(overrides, dict):
        return masked
    for role, cfg in overrides.items():
        if not isinstance(cfg, dict):
            masked[role] = cfg
            continue
        item = dict(cfg)
        if item.get("api_key"):
            item["api_key"] = _mask_api_key(item["api_key"])
        masked[role] = item
    return masked


def _public_llm_config(row: dict) -> dict:
    public = dict(row)
    public["apiKey"] = _mask_api_key(row.get("apiKey", ""))
    public["roleOverrides"] = _mask_role_overrides(row.get("roleOverrides") or {})
    return public


def _list_llm_configs() -> list[dict]:
    return [_public_llm_config(config) for config in llm_dao.list_configs()]


@router.get("/llm-configs")
async def api_list_llm_configs(_: str = Depends(require_maintainer)):
    return ApiResponse(data=_list_llm_configs())


@router.get("/role-profiles")
async def api_list_role_profiles(_: str = Depends(require_maintainer)):
    """返回角色档位的默认配置与说明（供前端"角色档位"配置区渲染）。"""
    profiles = []
    for role in ALL_ROLES:
        default = DEFAULT_ROLE_PROFILES.get(role, {})
        profiles.append(
            {
                "role": role,
                "label": ROLE_LABELS.get(role, role),
                "description": ROLE_DESCRIPTIONS.get(role, ""),
                "defaultTemperature": default.get("temperature"),
            }
        )
    return ApiResponse(data={"roles": profiles})


@router.post("/llm-configs")
async def api_create_llm_config(payload: LLMConfigCreate, _: str = Depends(require_maintainer)):
    today = utc_today()
    config_id = gen_id()

    if payload.isActive:
        llm_dao.deactivate_all_configs()

    try:
        stored_api_key = encrypt_secret(payload.apiKey.strip())
        normalized_overrides = _normalize_role_overrides(payload.roleOverrides)
        for cfg in normalized_overrides.values():
            if isinstance(cfg, dict) and cfg.get("api_key"):
                cfg["api_key"] = encrypt_secret(cfg["api_key"])
    except SecretBoxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    llm_dao.create_config({
        "id": config_id,
        "provider": payload.provider.strip(),
        "api_key": stored_api_key,
        "api_base": payload.apiBase.strip(),
        "model": payload.model.strip(),
        "temperature": payload.temperature,
        "max_tokens": payload.maxTokens,
        "role_overrides": json_dump(normalized_overrides),
        "is_active": 1 if payload.isActive else 0,
        "created_at": today,
        "updated_at": today,
    })

    config = llm_dao.get_config(config_id)
    return ApiResponse(data=_public_llm_config(config))


@router.put("/llm-configs/{config_id}/role-overrides")
async def api_update_llm_config_role_overrides(config_id: str, payload: RoleOverridesUpdate, _: str = Depends(require_maintainer)):
    """更新指定配置的角色级模型覆盖（建议1 写作档位前端配套：写入 roleOverrides）。

    第 2 档：roleOverrides 支持角色独立 api_key / api_base；写时掩码值（含 `*`）表示
    「保留原值」（前端回显脱敏后原样提交），空字符串表示「清除该角色 key」。
    全量替换语义：传空对象清除全部覆盖。
    """
    config = llm_dao.get_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="LLM 配置未找到。")

    normalized = _normalize_role_overrides(payload.roleOverrides)
    existing = config.get("roleOverrides")
    existing = existing if isinstance(existing, dict) else {}

    # 掩码 api_key 还原为原值；空 api_key 清除
    for role, cfg in normalized.items():
        if not isinstance(cfg, dict) or "api_key" not in cfg:
            continue
        submitted = cfg["api_key"]
        prev = existing.get(role)
        prev_key = prev.get("api_key") if isinstance(prev, dict) else ""
        if _is_masked_api_key(submitted):
            if prev_key:
                normalized[role] = {**cfg, "api_key": prev_key}
            else:
                normalized[role] = {k: v for k, v in cfg.items() if k != "api_key"}
        elif submitted == "":
            normalized[role] = {k: v for k, v in cfg.items() if k != "api_key"}

    try:
        for cfg in normalized.values():
            if isinstance(cfg, dict) and cfg.get("api_key") and not _is_masked_api_key(cfg["api_key"]):
                cfg["api_key"] = encrypt_secret(cfg["api_key"])
    except SecretBoxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    llm_dao.update_role_overrides(config_id, json_dump(normalized), utc_today())

    updated = llm_dao.get_config(config_id)
    return ApiResponse(data=_public_llm_config(updated))


@router.post("/llm-configs/{config_id}/activate")
async def api_activate_llm_config(config_id: str, _: str = Depends(require_maintainer)):
    today = utc_today()
    config = llm_dao.get_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="LLM 配置未找到。")

    llm_dao.deactivate_all_configs()
    llm_dao.activate_config(config_id, today)

    updated = llm_dao.get_config(config_id)
    return ApiResponse(data=_public_llm_config(updated))


@router.delete("/llm-configs/{config_id}")
async def api_delete_llm_config(config_id: str, _: str = Depends(require_maintainer)):
    config = llm_dao.get_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="LLM 配置未找到。")

    was_active = config.get("isActive")
    llm_dao.delete_config(config_id)

    if was_active:
        next_config = llm_dao.get_next_active_candidate()
        if next_config:
            llm_dao.activate_config(next_config["id"], utc_today())

    return ApiResponse(data={"id": config_id})


def _llm_health_snapshot() -> dict:
    """Read configuration state without making a billable provider request."""
    llm._refresh_runtime_config(force=True)
    configured = llm.is_configured()
    return {
        "configured": configured,
        "provider": llm.provider,
        "model": llm.model,
        "checked": False,
        "ok": False,
        "error": "" if configured else "当前没有可用的模型配置。",
    }


@router.get("/llm-health")
async def api_llm_health(_: str = Depends(require_maintainer)):
    return ApiResponse(data=_llm_health_snapshot())


@router.post("/llm-health")
async def api_probe_llm_health(_: str = Depends(require_maintainer)):
    result = _llm_health_snapshot()
    result["checked"] = True
    if not result["configured"]:
        return ApiResponse(data=result)

    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": "你是中文写作助手。"},
                {"role": "user", "content": "请只回复“连接正常”。"},
            ],
            max_tokens=32,
            thinking="disabled",
            timeout=15,
        )
        result["ok"] = bool(reply.strip())
    except Exception:  # noqa: BLE001 - upstream exception text can contain credentials or private URLs
        result["error"] = "模型连接检查失败，请稍后重试。"

    return ApiResponse(data=result)
