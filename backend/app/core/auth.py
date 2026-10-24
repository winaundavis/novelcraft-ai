"""Small invite-only identity and project ownership boundary."""

from fastapi import HTTPException, Request

from app.core.config import is_production_environment, settings
from app.dao import projects as projects_dao
from app.dao import users
from app.services import invite_service


def _invite_map() -> dict[str, str]:
    result: dict[str, str] = {}
    for item in settings.invite_tokens.split(","):
        token, separator, user_id = item.strip().partition(":")
        if separator and token and user_id:
            result[token] = user_id
    return result


def current_user_id(request: Request) -> str:
    session_user = users.user_for_session(request.cookies.get("novelcraft_session"))
    if session_user:
        return session_user["id"]
    token = request.headers.get("X-NovelCraft-Invite", "").strip()
    if token:
        # 邀请码当凭据：老 `.env` 码走老行为（免密码直接进，Q2=A）；
        # 库里 `bound` 的码进它绑定到的账号（Q1=C）；`pending`/`disabled` 一律不认。
        # 语义判断集中在 invite_service，异常也会被它吞成 invalid，不让表损坏影响老通道。
        kind, user_id = invite_service.resolve_credential(token)
        if kind == invite_service.CREDENTIAL_LEGACY and user_id:
            # Legacy environment mappings remain available for local
            # compatibility. In production they must point at a real account
            # so a copied placeholder cannot impersonate an ownerless user.
            if users.get_user(user_id) or not is_production_environment():
                return user_id
        if kind == invite_service.CREDENTIAL_BOUND and user_id and users.get_user(user_id):
            return user_id
    if settings.debug:
        return "local-dev"
    raise HTTPException(status_code=401, detail="请先提供有效的邀请码。")

def current_user(request: Request) -> dict | None:
    """Return the database identity when a session is present."""
    return users.user_for_session(request.cookies.get("novelcraft_session"))

def is_admin_user(user_id: str) -> bool:
    account = users.get_user(user_id)
    allowed = {item.strip() for item in settings.admin_user_ids.split(",") if item.strip()}
    return bool((account and account.get("is_admin")) or user_id in allowed)


def require_authenticated_user(request: Request) -> str:
    """Common dependency for non-project, authenticated API surfaces."""
    return current_user_id(request)


def require_maintainer(request: Request) -> str:
    user_id = require_authenticated_user(request)
    allowed = {item.strip() for item in settings.admin_user_ids.split(",") if item.strip()}
    if not is_admin_user(user_id):
        raise HTTPException(status_code=403, detail="无权管理服务器模型配置。")
    return user_id


def require_project_owner(request: Request, project_id: str) -> str:
    user_id = current_user_id(request)
    owner_id = projects_dao.get_project_owner(project_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="项目未找到。")
    if owner_id != user_id and not is_admin_user(user_id):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    # Bind billing/audit only after ownership succeeds. Both project-path and
    # body-project API routes use this guard, including services that only set
    # a role and otherwise inherit their request's project context.
    from app.llm.audit import set_llm_audit_context

    set_llm_audit_context(project_id=project_id)
    return user_id
