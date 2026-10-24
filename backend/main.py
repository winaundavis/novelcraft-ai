"""NovelCraft AI backend entrypoint."""

import json
import os
import socket
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import admin, ai, chapter_contracts, chapters, character_voice, copilot, foreshadows, fulltext, imports, novel_jobs, outline_candidates, projects, readiness, reference_works, review, rewrites, settings, story_state, style, writing_experiences
from app.db import init_db
from app.api import chat_conversations, chapter_trash, auth
from app.dao.chat_conversations import recover_pending
from app.core.config import (
    production_config_errors,
    is_production_environment,
    resolve_cors_origins,
    resolve_data_dir,
    resolve_database_path,
    settings as app_settings,
)
from app.core.auth import require_maintainer, require_project_owner
from app.core.csrf import csrf_request_allowed
from app.core.secret_box import decrypt_secret
from app.core.production_logging import build_request_event, configure_production_logging, log_request
from app.llm.audit import reset_llm_audit_context, set_llm_audit_context
from app.services import run_context
from app.services.novel_generation_service import recover_jobs_after_restart


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # P3：以 lifespan 替代已弃用的 @app.on_event("startup")
    init_db()
    if not app_settings.debug:
        configure_production_logging(app_settings.log_level)
    recover_jobs_after_restart()
    recover_pending()
    yield


app = FastAPI(
    title="NovelCraft AI API",
    description="Backend service for the NovelCraft AI writing workspace.",
    version="1.0.0",
    lifespan=lifespan,
)
_request_windows: dict[str, list[float]] = {}


def _is_rate_limit_exempt(request: Request) -> bool:
    """Exclude high-frequency read-only job polling from the mutation limiter.

    The frontend polls these endpoints while a job is running. Counting those
    reads against the same IP bucket as generation can starve the next real
    operation before it reaches the budget/provider guards.
    """
    if request.method != "GET":
        return False
    path = request.url.path
    return (
        path.startswith("/api/ai/project/")
        or path.startswith("/api/ai/") and path.endswith("/events")
        or path.startswith("/api/projects/") and "/chapters" in path
        or path.startswith("/api/projects/") and "/chat-conversations" in path
    )


# 登录与注册使用**各自独立**的 IP 窗口：
# - 都独立于浏览流量（否则用户刷新几次后连登录都会被 429 锁死）；
# - **也彼此独立**：同一出口 IP 上别人登录失败不应把新用户注册锁死
#   （共享 IP / 反代场景下这很常见）。
_auth_windows: dict[str, list[float]] = {}
_register_windows: dict[str, list[float]] = {}
_AUTH_ATTEMPT_PATHS = {"/api/auth/login"}
_REGISTER_ATTEMPT_PATHS = {"/api/auth/register"}


def _is_auth_attempt(request: Request) -> bool:
    return request.method == "POST" and request.url.path in _AUTH_ATTEMPT_PATHS


def _is_register_attempt(request: Request) -> bool:
    return request.method == "POST" and request.url.path in _REGISTER_ATTEMPT_PATHS


def _window_exceeded(windows: dict[str, list[float]], request: Request) -> bool:
    """按 IP 记录尝试次数；超过 `RATE_LIMIT` 次/60 秒返回 True。"""
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = [stamp for stamp in windows.get(key, []) if now - stamp < 60]
    if len(window) >= app_settings.rate_limit:
        windows[key] = window
        return True
    window.append(now)
    windows[key] = window
    return False


def _auth_attempt_exceeded(request: Request) -> bool:
    return _window_exceeded(_auth_windows, request)


def _register_attempt_exceeded(request: Request) -> bool:
    return _window_exceeded(_register_windows, request)

_cors_origins = resolve_cors_origins(app_settings.cors_allow_origins, debug=app_settings.debug)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
_allowed_hosts = [item.strip() for item in app_settings.allowed_hosts.split(",") if item.strip()]
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_allowed_hosts or ["*"],
)


@app.exception_handler(HTTPException)
async def production_http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    # 只改写**字符串** detail。结构化 detail（dict）是有意给前端区分的语义
    # （例如注册失败的 503 / 邀请码两类 403），改写成"AI 服务不可用"会误导用户。
    if not app_settings.debug and isinstance(detail, str):
        if exc.status_code in {502, 503}:
            # 这些状态码也用于注册、导入和配置服务；把每个 502/503
            # 都称为 AI 故障会误导用户，且无法帮助维护者区分实际入口。
            detail = "服务暂时不可用，请稍后重试。"
        elif exc.status_code >= 500:
            detail = "服务器内部错误。"
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)


@app.exception_handler(Exception)
async def production_unhandled_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误。"})


@app.middleware("http")
async def enforce_project_path_ownership(request: Request, call_next):
    started = time.perf_counter()
    response = None
    parts = request.url.path.strip("/").split("/")
    # T5：为每个 HTTP 请求建立显式 run 作用域，使该请求（及其派生的后台任务）内的
    # LLM 调用共享同一 run_id；调用方可通过响应头 X-NovelCraft-Run-Id 读取它，
    # 再用 /api/ai/llm-audit?runId=... 查询"本次"审计。未带该头的请求由服务端生成。
    incoming_run_id = request.headers.get("X-NovelCraft-Run-Id", "").strip()
    active_run_id, run_token = run_context.begin_run(incoming_run_id or None)
    # Never inherit a previous caller's billing identity. Ownership guards bind
    # the verified project downstream; streaming/background child tasks retain
    # that context independently when this middleware restores its own token.
    audit_token = set_llm_audit_context(merge=False)
    try:
        if not csrf_request_allowed(request):
            response = JSONResponse(status_code=403, content={"detail": "请求来源校验失败。"})
            return response
        if not app_settings.debug and _is_register_attempt(request):
            # 注册单独计数：别人在同一出口 IP 上反复登录失败，不应把新用户注册锁死。
            if _register_attempt_exceeded(request):
                response = JSONResponse(status_code=429, content={"detail": "注册尝试过于频繁，请稍后再试。"})
                return response
        elif not app_settings.debug and _is_auth_attempt(request):
            # 登录尝试单独计数：既防暴力破解，又不被浏览流量挤占（避免登录被 429 锁死）。
            if _auth_attempt_exceeded(request):
                response = JSONResponse(status_code=429, content={"detail": "登录尝试过于频繁，请稍后再试。"})
                return response
        elif (
            not app_settings.debug
            and request.url.path.startswith("/api/")
            and request.url.path not in {app_settings.health_path, "/api/ready"}
            and not _is_rate_limit_exempt(request)
        ):
            key = request.client.host if request.client else "unknown"
            now = time.monotonic()
            window = [stamp for stamp in _request_windows.get(key, []) if now - stamp < 60]
            if len(window) >= app_settings.rate_limit:
                response = JSONResponse(status_code=429, content={"detail": "请求过于频繁，请稍后再试。"})
                return response
            window.append(now)
            _request_windows[key] = window
        if len(parts) >= 3 and parts[0:2] == ["api", "projects"] and parts[2]:
            try:
                require_project_owner(request, parts[2])
            except HTTPException as exc:
                if app_settings.debug and exc.status_code == 404:
                    response = await call_next(request)
                    return response
                response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
                return response
        response = await call_next(request)
        return response
    finally:
        reset_llm_audit_context(audit_token)
        run_context.end_run(run_token)
        if response is not None:
            try:
                response.headers["X-NovelCraft-Run-Id"] = active_run_id
            except Exception:  # noqa: BLE001  响应已开始发送时不阻塞主流程
                pass
        if not app_settings.debug and request.url.path.startswith("/api/"):
            from app.core.auth import current_user_id

            try:
                user_id = current_user_id(request)
            except HTTPException:
                user_id = ""
            event = build_request_event(
                request_id=request.headers.get("X-Request-ID", "") or uuid.uuid4().hex,
                user_id=user_id,
                project_id=parts[2] if len(parts) >= 3 and parts[:2] == ["api", "projects"] else "",
                operation=f"{request.method} {request.url.path}",
                status=response.status_code if response is not None else 500,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            log_request(configure_production_logging(app_settings.log_level), event)

app.include_router(projects.router, prefix="/api")
app.include_router(chat_conversations.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(chapter_trash.router, prefix="/api")
app.include_router(chapters.router, prefix="/api")
app.include_router(chapter_contracts.router, prefix="/api")
app.include_router(story_state.router, prefix="/api")
app.include_router(readiness.router, prefix="/api")
app.include_router(character_voice.router, prefix="/api")
app.include_router(writing_experiences.router, prefix="/api")
app.include_router(foreshadows.router, prefix="/api")
app.include_router(reference_works.router, prefix="/api")
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(novel_jobs.router, prefix="/api/ai")
app.include_router(review.router, prefix="/api")
app.include_router(imports.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(style.router, prefix="/api")
app.include_router(fulltext.router, prefix="/api")
app.include_router(rewrites.router, prefix="/api")
app.include_router(copilot.router, prefix="/api")
app.include_router(outline_candidates.router, prefix="/api")
app.include_router(admin.router, prefix="/api")


@app.get(app_settings.health_path)
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "1.0.0"}


def _readiness_state() -> tuple[bool, list[str]]:
    """Return a safe readiness result and error codes.

    Public readiness only exposes the boolean result. The codes are consumed
    by the maintainer-only diagnostic endpoint below and never include paths,
    environment values, credentials, or database contents.
    """
    errors = list(production_config_errors(app_settings))
    data_dir = resolve_data_dir(app_settings.data_dir)
    db_path = resolve_database_path(
        data_dir,
        app_settings.novelcraft_db_path or os.getenv("NOVELCRAFT_DB_PATH", ""),
    )
    try:
        if not db_path.exists() or not db_path.is_file():
            errors.append("database_not_ready")
    except OSError:
        errors.append("database_not_ready")
    try:
        if not data_dir.exists() or not data_dir.is_dir() or not os.access(data_dir, os.W_OK):
            errors.append("storage_not_ready")
    except OSError:
        errors.append("storage_not_ready")
    if is_production_environment(app_settings) and not any(
        code in errors for code in ("database_not_ready", "storage_not_ready")
    ):
        configured_admin_ids = [
            item.strip()
            for item in str(app_settings.admin_user_ids or "").split(",")
            if item.strip()
        ]
        try:
            # Read the configured database directly. Calling the global DAO
            # here would use the import-time DB_PATH when a deployment
            # overrides NOVELCRAFT_DB_PATH after import, and could even create
            # a different database during a readiness probe.
            uri = db_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                rows = conn.execute(
                    "SELECT id FROM users WHERE id IN ({})".format(
                        ",".join("?" for _ in configured_admin_ids) or "NULL"
                    ),
                    tuple(configured_admin_ids),
                ).fetchall()
                admin_initialized = bool(rows) or conn.execute(
                    "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
                ).fetchone() is not None
        except Exception:  # noqa: BLE001 - readiness must fail closed without exposing DB details
            admin_initialized = False
        if not admin_initialized:
            errors.append("admin_not_initialized")
        try:
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                for api_key, role_json in conn.execute("SELECT api_key, role_overrides FROM llm_configs"):
                    decrypt_secret(api_key)
                    roles = json.loads(role_json)
                    if not isinstance(roles, dict):
                        raise ValueError("invalid role overrides")
                    for cfg in roles.values():
                        if not isinstance(cfg, dict):
                            raise ValueError("invalid role override")
                        decrypt_secret(cfg.get("api_key"))
        except Exception:  # noqa: BLE001 - return codes only, never credential or DB contents
            errors.append("credentials_not_ready")
    return not errors, sorted(set(errors))


@app.get("/api/ready")
async def readiness() -> JSONResponse:
    """Deployment readiness probe with a deliberately minimal public body."""
    ready, _errors = _readiness_state()
    if not ready:
        return JSONResponse(status_code=503, content={"status": "not_ready", "version": "1.0.0"})
    return JSONResponse(status_code=200, content={"status": "ready", "version": "1.0.0"})


@app.get("/api/health/internal")
async def internal_health(_: str = Depends(require_maintainer)) -> dict[str, object]:
    """Maintainer-only diagnostics; never expose these fields publicly."""
    ready, config_errors = _readiness_state()
    data_dir = resolve_data_dir(app_settings.data_dir)
    db_path = resolve_database_path(data_dir, app_settings.novelcraft_db_path or os.getenv("NOVELCRAFT_DB_PATH", ""))
    try:
        database_ready = db_path.exists() and db_path.is_file()
    except OSError:
        database_ready = False
    try:
        storage_ready = data_dir.exists() and data_dir.is_dir() and os.access(data_dir, os.W_OK)
    except OSError:
        storage_ready = False
    provider_configured = any(
        bool(getattr(app_settings, name, None))
        for name in ("deepseek_api_key", "arts_api_key", "openai_api_key", "anthropic_api_key", "qwen_api_key", "kimi_api_key", "glm_api_key")
    )
    return {
        "status": "ok" if ready and database_ready and storage_ready else "degraded",
        "config_ready": not bool(config_errors),
        "config_errors": config_errors,
        "provider_configured": provider_configured,
        "database_ready": database_ready,
        "storage_ready": storage_ready,
    }


# ── 可选：单进程同时提供已构建的前端（部署用，默认关闭） ──────────────────
# 设置 NOVELCRAFT_STATIC_DIR 指向 `frontend/dist` 后，后端在**同一个源**上同时提供
# 静态站点与 /api。这样部署不需要额外反向代理：少一个组件，也天然不存在代理
# 缓冲把 SSE 憋到最后一次性吐出"的问题。不设置该变量时行为与之前完全一致。
_static_dir = os.getenv("NOVELCRAFT_STATIC_DIR", "").strip()
if _static_dir:
    _static_root = Path(_static_dir).expanduser().resolve()
    _static_index = _static_root / "index.html"
    if _static_index.is_file():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        _assets_dir = _static_root / "assets"
        if _assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str) -> FileResponse:
            # 未匹配到的 /api 路径必须返回 404，不能被 SPA 回落吞成 index.html，
            # 否则前端会把 HTML 当成 JSON 解析，报出难以定位的错误。
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = (_static_root / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(_static_root):
                return FileResponse(candidate)
            return FileResponse(_static_index)
    else:
        print(
            f"[告警] NOVELCRAFT_STATIC_DIR={_static_dir} 下没有 index.html，静态托管已跳过。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    port = int(os.getenv("NOVELCRAFT_PORT", "8000"))
    # P1-2 单实例启动约束：检测端口冲突并明确告警（而非静默双开两套后端）
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(
                f"[告警] 端口 {port} 已被占用：可能已有一个 NovelCraft 后端在运行。\n"
                f"        如需重启，请先结束旧进程，或设置 NOVELCRAFT_PORT 更换端口。",
                file=sys.stderr,
            )
    # 默认关闭 reload（开发外场景避免残留双后端）；开发时可用 NOVELCRAFT_RELOAD=1 开启
    reload_enabled = os.getenv("NOVELCRAFT_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload_enabled)
