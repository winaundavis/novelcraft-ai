"""账号注册 · 登录 · 邀请码绑定。

所有"这个码现在是什么状态、能不能注册、能不能登录"的判断都收在
`app.services.invite_service`，本模块只负责 HTTP 映射（PRD 3.2 的状态码与文案、
3.4 的登录分流、Q4 的"注册成功即自动登录"）。
"""
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, StrictBool
from app.dao import users
from app.models.schemas import ApiResponse
from app.core.config import settings
from app.core.auth import current_user_id, is_admin_user
from app.core.terms import TERMS_REQUIRED_MESSAGE, TERMS_UPDATED_MESSAGE, TERMS_VERSION
from app.services import invite_service

router=APIRouter(prefix='/auth', tags=['auth'])
COOKIE='novelcraft_session'
SESSION_MAX_AGE = 14 * 86400
class Credentials(BaseModel):
    # 刻意不用 pydantic 的长度约束：框架的超长/格式错误会返回 **422**，
    # 而 422 的 `detail` 是**数组**，且会把用户提交的**密码明文**回显在 `input` 字段里
    # （会进入前端提示与代理日志）。这里一律放开，由 invite_service 统一返回
    # **400 + 字符串 detail**，与其它错误同一个契约。
    username: str = ''
    password: str = ''
    invite: str = ''
    # 只认可明确的 JSON true，不把字符串或数字转换成同意。
    termsAccepted: StrictBool = False
    termsVersion: str = ''


# 前端靠这两个 code 区分"码写错了"（必须留在弹层让用户改）
# 与"老码映射的账号行不存在"（回落到请求头带码的遗留通道）。
CODE_INVITE_INVALID = 'invite_invalid'
CODE_LEGACY_ACCOUNT_MISSING = 'legacy_account_missing'


def _require_current_terms(data: Credentials) -> None:
    if not data.termsAccepted:
        raise HTTPException(400, TERMS_REQUIRED_MESSAGE)
    if data.termsVersion != TERMS_VERSION:
        raise HTTPException(409, TERMS_UPDATED_MESSAGE)


def _set_session_cookie(response: Response, user_id: str) -> None:
    """种会话 cookie。写法与既有 login 完全一致（前端按这个行为判断已登录）。"""
    token = users.create_session(user_id)
    response.set_cookie(COOKIE, token, httponly=True, samesite='lax',
                        secure=settings.auth_cookie_secure, max_age=SESSION_MAX_AGE, path='/')

def _account_payload(user: dict) -> dict:
    # 管理员判定**必须**与 `require_maintainer` 共用 `is_admin_user`：
    # 只读 `users.is_admin` 会漏掉 `.env` 的 `ADMIN_USER_IDS` 与 DEBUG 的 `local-dev`，
    # 于是出现「接口放行、菜单却不显示」的口径分裂。
    return {'id': user['id'], 'username': user['username'], 'isAdmin': is_admin_user(user['id'])}

def _authenticate_with_invite(invite: str) -> dict:
    """只提交邀请码时的登录分流（PRD 3.4）。返回账号；不可用则抛 HTTPException。

    **错误必须可区分**：
    - 码写错了 → 403 + `invite_invalid`（前端要留在弹层让用户改，绝不能放他进去）；
    - 码没被注册过 → 403 + `pending`（引导去注册）；
    - 码已停用 → 403 + `disabled`；
    - 老码映射/绑定到一个**不存在的账号** → 401 + `legacy_account_missing`
      （只有这种情况才允许前端回落到"请求头带码"的遗留通道）。
    """
    kind, user_id = invite_service.resolve_credential(invite)
    if kind == invite_service.CREDENTIAL_PENDING:
        raise HTTPException(403, invite_service.PENDING_LOGIN_MESSAGE)
    if kind == invite_service.CREDENTIAL_DISABLED:
        raise HTTPException(403, invite_service.DISABLED_LOGIN_MESSAGE)
    if kind == invite_service.CREDENTIAL_EXPIRED:
        raise HTTPException(403, invite_service.EXPIRED_LOGIN_MESSAGE)
    if kind in (invite_service.CREDENTIAL_BOUND, invite_service.CREDENTIAL_LEGACY) and user_id:
        account = users.get_user(user_id)
        if account:
            return account
        # 老码指向的账号行不存在：唯一允许回落的场景
        raise HTTPException(
            401,
            {
                'code': CODE_LEGACY_ACCOUNT_MISSING,
                'message': invite_service.LEGACY_ACCOUNT_MISSING_MESSAGE,
            },
        )
    raise HTTPException(
        403,
        {'code': CODE_INVITE_INVALID, 'message': invite_service.INVALID_LOGIN_MESSAGE},
    )

@router.post('/register')
async def register(data: Credentials, response: Response):
    """邀请码 + 用户名 + 密码 注册；成功后自动登录（Q4）。

    邀请码**只在注册真正成功时**才被绑定：用户名重复/密码不合规等失败路径
    一律不消耗码（PRD 验收 3）。
    """
    # 先挡"输入过大"：否则框架会返回 422（detail 是数组，且会回显密码明文）。
    ok, reason = invite_service.validate_input_sizes(data.username, data.password, data.invite)
    if not ok:
        raise HTTPException(400, reason)
    kind, invite_row = invite_service.check_invite_for_registration(data.invite)
    if kind != 'ok':
        status, message = invite_service.register_error(kind)
        raise HTTPException(status, message)
    ok, reason = invite_service.validate_username(data.username)
    if not ok:
        raise HTTPException(400, reason)
    ok, reason = invite_service.validate_password(data.username, data.password)
    if not ok:
        raise HTTPException(400, reason)
    username = invite_service.normalize_username(data.username)
    if users.find_user(username):
        raise HTTPException(409, invite_service.USERNAME_TAKEN_MESSAGE)
    # 条款检查放在所有输入校验**之后**：保证"码错/用户名占用"等既有错误码不变，
    # 只有当这次注册真的要落库时，才因为没勾选而拒绝。
    _require_current_terms(data)
    try:
        uid = users.create_user(username, data.password, terms_version=data.termsVersion)
    except sqlite3.IntegrityError as exc:
        # 仅用户名唯一约束冲突属于占用；协议字段/触发器等写入失败不能误报重名。
        if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
            raise HTTPException(409, invite_service.USERNAME_TAKEN_MESSAGE) from exc
        raise HTTPException(503, '注册服务暂时不可用，请稍后重试。') from exc
    except sqlite3.Error as exc:
        raise HTTPException(503, '注册服务暂时不可用，请稍后重试。') from exc
    # 绑定是原子条件更新：失败说明码刚被别人用掉（并发注册）。
    # 此时必须回滚刚建的账号——它还没有任何作品，删掉是安全的，否则会留下
    # 一个"注册失败但账号已存在、且码不属于它"的僵尸账号。
    #
    # ``bind_invite_to_user`` 可能**抛异常**（库锁/表损坏），而不只是返回 False；
    # 只判返回值会让异常直接冒泡成 500，而账号已经建出来了——同样留下僵尸账号。
    try:
        bound = invite_service.bind_invite_to_user(invite_row['code_hash'], uid)
    except sqlite3.Error as exc:
        users.delete_user_if_no_owned_data(uid)
        raise HTTPException(503, '注册服务暂时不可用，请稍后重试。') from exc
    if not bound:
        users.delete_user_if_no_owned_data(uid)
        status, message = invite_service.register_error('used')
        raise HTTPException(status, message)
    _set_session_cookie(response, uid)
    account = users.get_user(uid) or {'id': uid, 'username': username, 'is_admin': 0}
    return ApiResponse(data=_account_payload(account))

@router.post('/login')
async def login(data: Credentials, response: Response):
    """用户名+密码，或只提交邀请码（PRD 3.4）。"""
    ok, reason = invite_service.validate_input_sizes(data.username, data.password, data.invite)
    if not ok:
        raise HTTPException(400, reason)
    username = invite_service.normalize_username(data.username)
    if username and data.password:
        user = users.find_user(username)
        if user and users.verify_password(data.password, user['password_hash']):
            # 先验证身份，再检查当前协议；旧账号也可通过明确同意补齐记录。
            _require_current_terms(data)
            users.record_terms_acceptance(user['id'], data.termsVersion)
            _set_session_cookie(response, user['id'])
            return ApiResponse(data=_account_payload(user))
    if data.invite.strip():
        user = _authenticate_with_invite(data.invite)
        _require_current_terms(data)
        users.record_terms_acceptance(user['id'], data.termsVersion)
        _set_session_cookie(response, user['id'])
        return ApiResponse(data=_account_payload(user))
    raise HTTPException(401, '账户名或密码错误。')

@router.get('/username-available')
async def username_available(username: str = ''):
    """注册表单失焦时查询用户名占用状态（PRD 3.3）。

    不合规的用户名也算"不可用"，并带上原因，前端可以直接提示而不必自己复刻规则。
    该接口代价是"可枚举用户名"，这是注册表单的固有取舍；它落在 `/api/` 的既有
    IP 限流窗口内（未被 `_is_rate_limit_exempt` 豁免）。
    """
    ok, reason = invite_service.validate_username(username)
    if not ok:
        return ApiResponse(data={'available': False, 'reason': reason})
    # 必须与注册用**同一个口径**（normalize 后再查），否则会出现
    # "表单说可用、提交却 409"的谎报。
    if users.find_user(invite_service.normalize_username(username)) is not None:
        return ApiResponse(data={'available': False, 'reason': invite_service.USERNAME_TAKEN_MESSAGE})
    return ApiResponse(data={'available': True, 'reason': ''})

@router.get('/invites/mine')
async def my_invites(request: Request):
    """当前账号已绑定的邀请码：hint + 绑定时间 + 备注，**不回显明文**（库里只有哈希）。

    用途：让用户知道手上那把"备用钥匙"是哪一把（PRD 3.6）。没有则返回空数组。
    """
    user = users.user_for_session(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, '请先登录。')
    return ApiResponse(data=invite_service.bound_invites_for_user(user['id']))

@router.post('/logout')
async def logout(request: Request, response: Response):
    users.delete_session(request.cookies.get(COOKIE)); response.delete_cookie(COOKIE,path='/'); return ApiResponse(data=None)
@router.get('/me')
async def me(request: Request):
    user=users.user_for_session(request.cookies.get(COOKIE))
    if not user: raise HTTPException(401,'请先登录。')
    return ApiResponse(data={'id':user['id'],'username':user['username'],'isAdmin':is_admin_user(user['id'])})

@router.get('/permissions')
async def permissions(request: Request):
    """当前凭据能做什么。**与 `require_maintainer` 用同一套解析与判定**。

    为什么不能只看 `/auth/me`：`me` 只认会话 cookie，而老 `.env` 邀请码走的是
    请求头通道（没有会话），前端就无从得知「模型设置」该不该出现在菜单里。
    这里用 `current_user_id`（cookie 或邀请码头都能解析）配合 `is_admin_user`，
    保证**前端显示什么**与**后端放行什么**出自同一个判断。
    """
    user_id = current_user_id(request)
    return ApiResponse(data={'userId': user_id, 'isAdmin': is_admin_user(user_id)})

@router.delete('/account')
async def delete_account(request: Request, response: Response):
    """注销账号。

    政策：用户名下仍有自有项目（含回收站数据）时**拒绝**注销，并告知数量与处理方式；
    没有作品的账号正常注销并撤销全部会话。统计严格按 ``projects.owner_id``，
    管理员的全项目访问权限不算"拥有"，也不会把自己的账号与别人的作品绑定。
    """
    user = users.user_for_session(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, '请先登录。')
    result = users.delete_user_if_no_owned_data(user['id'])
    if not result['deleted']:
        parts = []
        if result['projects']:
            parts.append(f"{result['projects']} 个项目（其中回收站章节 {result['trashed_chapters']} 条）")
        if result.get('reference_works'):
            parts.append(f"{result['reference_works']} 份参考档案")
        if result.get('outline_candidates'):
            parts.append(f"{result['outline_candidates']} 条大纲候选")
        raise HTTPException(409, detail={
            'code': 'account_has_owned_projects',
            'message': (
                f"无法注销：该账号名下仍有 {'、'.join(parts) or '数据'}。"
                "请先导出或删除这些内容，再回来注销。"
            ),
            'projects': result['projects'],
            'trashedChapters': result['trashed_chapters'],
            'referenceWorks': result.get('reference_works', 0),
            'outlineCandidates': result.get('outline_candidates', 0),
        })
    response.delete_cookie(COOKIE, path='/')
    return ApiResponse(data=None)
