"""邀请码语义中心（PRD 3.1 状态机 / 3.2 注册校验 / 3.3 用户名密码规则 / 3.4 登录）。

为什么单独一层：注册接口、登录接口、`current_user_id`（老 header 通道）与命令行脚本
必须共用**同一套**判断，否则很容易出现"脚本说能用、接口说不行"的分裂。

异常与降级（PRD 四）：邀请码表缺失/损坏时，DB 码一律不认（403 而不是 500），
但**老 `.env` 通道优先判定**，所以校验异常不会把作者自己锁在门外。
"""

from __future__ import annotations

import re
import sqlite3

from app.dao import invites as invites_dao


# ── 凭据状态（resolve_credential 的返回值） ──────────────
CREDENTIAL_LEGACY = "legacy"
CREDENTIAL_BOUND = "bound"
CREDENTIAL_PENDING = "pending"
CREDENTIAL_DISABLED = "disabled"
CREDENTIAL_EXPIRED = "expired"
CREDENTIAL_INVALID = "invalid"

# ── 文案（与 PRD 3.2 表格逐字一致，前端直接展示） ────────
REGISTER_ERRORS: dict[str, tuple[int, str]] = {
    "invalid": (403, "邀请码无效，请检查是否输错。"),
    "used": (403, "该邀请码已被使用，请向邀请人索取新码。"),
    "expired": (403, "该邀请码已过期，请向邀请人索取新码。"),
    "disabled": (403, "该邀请码已被停用。"),
}
USERNAME_TAKEN_MESSAGE = "该用户名已被占用，换一个吧。"
PASSWORD_RULE_MESSAGE = "密码至少 8 位，且不能与用户名相同。"
PENDING_LOGIN_MESSAGE = "这个邀请码还没被注册，请先注册。"
DISABLED_LOGIN_MESSAGE = "该邀请码已被停用。"
EXPIRED_LOGIN_MESSAGE = "该邀请码已过期。"
INVALID_LOGIN_MESSAGE = "邀请码无效，请检查是否输错。"
LEGACY_ACCOUNT_MISSING_MESSAGE = "邀请码对应的账号不存在，请联系管理员。"

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 30
PASSWORD_MIN_LENGTH = 8
# 上限只用于**拒绝过大的输入**（防止无意义的巨大请求体），不是业务规则。
PASSWORD_MAX_LENGTH = 512
INVITE_MAX_LENGTH = 200
USERNAME_INPUT_MAX_LENGTH = 200

# 允许：中文（基本区 + 扩展 A）、英文字母、数字、下划线、连字符。
# 用 fullmatch 整体匹配，因此空格、全角空格（U+3000）与其它符号天然被拒。
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]+")


def _legacy_invite_map() -> dict[str, str]:
    """`.env` 的 `码:用户id` 老通道。映射实现仍留在 `app.core.auth`（单一实现）。

    延迟导入是为了避免 `core.auth` ↔ `services.invite_service` 的循环依赖。
    """
    from app.core.auth import _invite_map

    return _invite_map()


def normalize_username(username: str | None) -> str:
    """用户名去首尾空白（注册、登录、占用查询**必须**都过这个函数）。

    **不做大小写折叠**：唯一性由 SQL `lower(username)` 索引负责。
    """
    return (username or "").strip()


def validate_input_sizes(username: str | None, password: str | None, invite: str | None) -> tuple[bool, str]:
    """把"输入过大"也收成 400 + 字符串 detail（而不是框架的 422）。

    422 的 detail 是数组，而且会把用户提交的**密码明文**回显在 `input` 里。
    """
    if len(normalize_username(username)) > USERNAME_INPUT_MAX_LENGTH:
        return False, "用户名过长。"
    if len(password or "") > PASSWORD_MAX_LENGTH:
        return False, f"密码过长（上限 {PASSWORD_MAX_LENGTH} 个字符）。"
    if len((invite or "").strip()) > INVITE_MAX_LENGTH:
        return False, "邀请码过长，请检查是否粘错了内容。"
    return True, ""


def validate_username(username: str | None) -> tuple[bool, str]:
    """用户名规则（PRD 3.3）。返回 (是否合规, 原因)。"""
    value = normalize_username(username)
    if len(value) < USERNAME_MIN_LENGTH or len(value) > USERNAME_MAX_LENGTH:
        return False, f"用户名需为 {USERNAME_MIN_LENGTH}–{USERNAME_MAX_LENGTH} 个字符（去首尾空白后）。"
    if not _USERNAME_PATTERN.fullmatch(value):
        return False, "用户名只能包含中文、字母、数字、下划线或连字符。"
    return True, ""


def validate_password(username: str | None, password: str | None) -> tuple[bool, str]:
    """密码规则（PRD 3.3）：≥8 位，且不能与用户名相同（大小写不敏感）。"""
    value = password or ""
    if len(value) < PASSWORD_MIN_LENGTH:
        return False, PASSWORD_RULE_MESSAGE
    if value.lower() == normalize_username(username).lower():
        return False, PASSWORD_RULE_MESSAGE
    return True, ""


def resolve_credential(code: str | None) -> tuple[str, str | None]:
    """把一个凭据判成 5 种状态之一：`(kind, user_id)`。

    - `legacy`  ：`.env` 老码 → 免密码直接进（Q2=A，行为与改造前完全一致）
    - `bound`   ：库里已绑定账号的单次码 → 进绑定账号（Q1=C）
    - `pending` ：还没被认领 → **不能登录**（没有归属账号，无从判断进谁的账号）
    - `disabled`：已停用 → 既不注册也不登录
    - `invalid` ：空 / 不存在 / 校验异常

    老通道**先判**：邀请码表出问题时也不能影响它。
    """
    raw = (code or "").strip()
    if not raw:
        return CREDENTIAL_INVALID, None
    legacy_user_id = _legacy_invite_map().get(raw)
    if legacy_user_id:
        return CREDENTIAL_LEGACY, legacy_user_id
    try:
        row = invites_dao.find_by_code(raw)
    except (sqlite3.Error, ValueError, TypeError):
        # 表不存在 / 库损坏 / 哈希异常：DB 码一律不认，但上面的老通道不受影响
        return CREDENTIAL_INVALID, None
    if row is None:
        return CREDENTIAL_INVALID, None
    # Shared invites authorize registration only. Their first registrant is
    # retained for audit, never as an identity that all code holders can enter.
    # Missing max_uses is the pre-migration one-time format; malformed values
    # fail closed rather than becoming an account credential.
    try:
        max_uses = int(row.get("max_uses", 1))
    except (TypeError, ValueError):
        return CREDENTIAL_INVALID, None
    if max_uses != 1:
        return CREDENTIAL_INVALID, None
    status = str(row.get("status") or "")
    if status == invites_dao.STATUS_BOUND:
        bound_user_id = str(row.get("bound_user_id") or "")
        # bound 却没有归属账号 = 数据损坏，不能拿来登录
        if invites_dao.is_expired(row):
            return CREDENTIAL_EXPIRED, None
        return (CREDENTIAL_BOUND, bound_user_id) if bound_user_id else (CREDENTIAL_INVALID, None)
    if status == invites_dao.STATUS_DISABLED:
        return CREDENTIAL_DISABLED, None
    if status == invites_dao.STATUS_PENDING:
        if invites_dao.is_expired(row):
            return CREDENTIAL_EXPIRED, None
        return CREDENTIAL_PENDING, None
    return CREDENTIAL_INVALID, None


def check_invite_for_registration(code: str | None) -> tuple[str, dict | None]:
    """注册前的邀请码校验，返回 `(kind, invite_row)`，kind ∈ ok/invalid/used/disabled。

    只有 `pending` 的**库内码**能注册。**老 `.env` 码要先判并直接拒**：
    它已经映射到一个真实身份，如果允许拿它注册，就会出现"用这把码登录进的是别人"
    这种双真值来源的矛盾（登录走老通道，注册却把码绑给了新账号）。
    """
    raw = (code or "").strip()
    if not raw:
        return "invalid", None
    if _legacy_invite_map().get(raw):
        return "invalid", None
    try:
        row = invites_dao.find_by_code(raw)
    except (sqlite3.Error, ValueError, TypeError):
        return "invalid", None
    if row is None:
        return "invalid", None
    status = str(row.get("status") or "")
    if status == invites_dao.STATUS_PENDING:
        if invites_dao.is_expired(row):
            return "expired", row
        if not invites_dao.uses_remaining(row):
            return "used", row
        return "ok", row
    if status == invites_dao.STATUS_BOUND:
        return "used", row
    if status == invites_dao.STATUS_DISABLED:
        return "disabled", row
    return "invalid", None


def register_error(kind: str) -> tuple[int, str]:
    """注册失败原因 → (HTTP 状态码, 文案)。未知原因按"无效"处理，绝不 500。"""
    return REGISTER_ERRORS.get(kind, REGISTER_ERRORS["invalid"])


def bind_invite_to_user(code_hash: str, user_id: str) -> bool:
    """把码绑定到账号（原子，见 DAO 注释）。"""
    return invites_dao.bind_invite(code_hash, user_id)


def bound_invites_for_user(user_id: str) -> list[dict]:
    """"我的邀请码"数据。表缺失时退化为空数组，不把账号页整个弄挂。"""
    if not user_id:
        return []
    try:
        return invites_dao.list_bound_invites(user_id)
    except (sqlite3.Error, ValueError, TypeError):
        return []
