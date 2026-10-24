"""邀请码持久化（PRD：账号注册 · 邀请码绑定 · 归属统一）。

设计要点（为什么这么做）
------------------------
- Q1=C 语义下**邀请码就是登录凭据**，泄露等于账号被拿，所以库里只存
  `sha256(码)`；明文只在生成时返回一次，此后任何读取路径都拿不到它。
- `code_hint` 只留后 4 位，够人肉核对"这是哪一把钥匙"，不足以还原明文。
- 状态机 `pending → bound / disabled`；绑定用**条件更新 + rowcount**实现原子性，
  并发注册同一个码时只有一个调用能成功（不会一码绑两人）。

本模块只做存储事实，不做"能不能注册/登录"的语义判断（那在
`app.services.invite_service`），避免两处口径不一致。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.db import get_conn
from app.models.schemas import gen_id


# 去掉易混字符（0/O/1/I/l）后剩 32 个：数字 2-9 + 大写字母（去掉 I/O）。
# 32 = 2**5，每位正好 5 bit 熵；12 位 ≈ 60 bit，手抄友好且不可枚举。
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 12
HINT_LENGTH = 4
MAX_BATCH = 100
MAX_USES = 1000

STATUS_PENDING = "pending"
STATUS_BOUND = "bound"
STATUS_DISABLED = "disabled"
VALID_STATUSES = (STATUS_PENDING, STATUS_BOUND, STATUS_DISABLED)

# 对外可见的列（**刻意排除 code_hash**）：即使调用方不小心打印整行，也拿不到哈希。
PUBLIC_COLUMNS = (
    "i.id, i.code_hint, i.label, i.status, "
    "i.bound_user_id, i.bound_at, i.created_at, i.created_by, "
    "i.expires_at, i.max_uses, i.used_count"
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_expires_at(value: str | datetime | None) -> str:
    """Normalize an expiry to a UTC ISO timestamp, or empty for no expiry."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError as exc:
            raise ValueError("expires_at 必须是带时区的 ISO-8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expires_at 必须包含时区（例如 2030-01-01T00:00:00Z）。")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise ValueError("expires_at 必须晚于当前时间。")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_expires_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_expired(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Malformed non-empty expiry values fail closed as expired."""
    expiry = str(row.get("expires_at") or "").strip()
    if not expiry:
        return False
    parsed = _parse_expires_at(expiry)
    return parsed is None or parsed <= (now or datetime.now(timezone.utc))


def uses_remaining(row: dict[str, Any]) -> bool:
    try:
        max_uses = int(row.get("max_uses", 1))
        used_count = int(row.get("used_count", 0))
    except (TypeError, ValueError):
        return False
    return 1 <= max_uses <= MAX_USES and 0 <= used_count < max_uses


def normalize_code(code: str | None) -> str:
    """统一大小写与首尾空白：用户手抄邀请码时不区分大小写。"""
    return (code or "").strip().upper()


def hash_code(code: str | None) -> str:
    """邀请码哈希集中在此处，DAO / 服务层 / 脚本共用同一口径。"""
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def generate_invites(
    count: int,
    label: str = "",
    created_by: str = "",
    *,
    expires_at: str | datetime | None = None,
    max_uses: int = 1,
) -> list[str]:
    """生成 `count` 个邀请码，返回**明文列表（唯一一次）**，库里只落哈希与 hint。"""
    if count < 1 or count > MAX_BATCH:
        raise ValueError(f"count 必须在 1–{MAX_BATCH} 之间。")
    if isinstance(max_uses, bool) or not isinstance(max_uses, int) or not 1 <= max_uses <= MAX_USES:
        raise ValueError(f"max_uses 必须是 1–{MAX_USES} 的整数。")
    expiry = normalize_expires_at(expires_at)
    now = _utc_iso()
    plaintext: list[str] = []
    with get_conn() as conn:
        while len(plaintext) < count:
            code = generate_code()
            try:
                conn.execute(
                    "INSERT INTO invites"
                    " (id, code_hash, code_hint, label, status, bound_user_id, bound_at, created_at, created_by,"
                    " expires_at, max_uses, used_count)"
                    " VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, 0)",
                    (
                        gen_id(),
                        hash_code(code),
                        code[-HINT_LENGTH:],
                        label,
                        STATUS_PENDING,
                        now,
                        created_by,
                        expiry,
                        max_uses,
                    ),
                )
            except sqlite3.IntegrityError:
                continue  # 哈希撞车（概率可忽略）：换一个码重试，绝不返回重复码
            plaintext.append(code)
    return plaintext


def find_by_code(code: str | None) -> dict | None:
    """按哈希查邀请码（大小写不敏感 + 去首尾空白）。返回整行（含 code_hash，供绑定用）。"""
    normalized = normalize_code(code)
    if not normalized:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invites WHERE code_hash = ?", (hash_code(normalized),)
        ).fetchone()
    return dict(row) if row else None


def list_invites() -> list[dict]:
    """列出发出的邀请码（**绝不含明文，也不含 code_hash**）。

    绑定账号名通过 LEFT JOIN 现查，避免把用户名冗余进邀请码表。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {PUBLIC_COLUMNS}, u.username AS bound_username FROM invites i"
            " LEFT JOIN users u ON u.id = i.bound_user_id"
            " ORDER BY i.created_at DESC, i.id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def list_bound_invites(user_id: str) -> list[dict]:
    """"我的邀请码"：该账号已绑定的码的 hint / label / 绑定时间（不含明文与哈希）。"""
    if not user_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code_hint, label, bound_at, expires_at, max_uses, used_count FROM invites"
            " WHERE bound_user_id = ? AND status = ?"
            " ORDER BY bound_at, created_at",
            (user_id, STATUS_BOUND),
        ).fetchall()
    return [dict(row) for row in rows]


def count_by_status() -> dict[str, int]:
    """pending / bound / disabled 三个计数（作者唯一需要盯的运营数字）。"""
    counts = {status: 0 for status in VALID_STATUSES}
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS total FROM invites GROUP BY status").fetchall()
    for row in rows:
        counts[str(row["status"])] = int(row["total"])
    return counts


def disable_invite(invite_id: str | None = None, hint: str | None = None) -> dict | None:
    """停用邀请码（pending / bound 都可停用）。已停用再停用是幂等的。

    `hint` 只有 4 位、可能撞车；撞车时不猜，直接报错让调用方改用 `--id`。
    """
    target_id = (invite_id or "").strip()
    hint_value = (hint or "").strip()
    if not target_id and not hint_value:
        raise ValueError("必须提供 invite_id 或 hint。")
    with get_conn() as conn:
        if not target_id:
            rows = conn.execute(
                "SELECT id FROM invites WHERE code_hint = ?", (hint_value,)
            ).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                raise ValueError("该 hint 对应多个邀请码，请改用 --id 精确指定。")
            target_id = str(rows[0]["id"])
        if conn.execute("SELECT 1 FROM invites WHERE id = ?", (target_id,)).fetchone() is None:
            return None
        conn.execute(
            "UPDATE invites SET status = ? WHERE id = ? AND status != ?",
            (STATUS_DISABLED, target_id, STATUS_DISABLED),
        )
        row = conn.execute(
            f"SELECT {PUBLIC_COLUMNS}, u.username AS bound_username FROM invites i"
            " LEFT JOIN users u ON u.id = i.bound_user_id WHERE i.id = ?",
            (target_id,),
        ).fetchone()
    return dict(row) if row else None


def bind_invite(code_hash: str, user_id: str) -> bool:
    """原子消耗一次邀请码额度并在达到上限时关闭。

    默认 max_uses=1 时保持原有「一码一人」行为；多次邀请码在达到
    max_uses 前保持 pending，首次使用者作为 bound_user_id 留痕。
    `UPDATE ... WHERE` 同时检查额度和有效期，保证并发注册不会超限。
    """
    if not code_hash or not user_id:
        return False
    now = _utc_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE invites SET "
            "status = CASE WHEN used_count + 1 >= max_uses THEN ? ELSE ? END, "
            "bound_user_id = CASE WHEN bound_user_id = '' THEN ? ELSE bound_user_id END, "
            "bound_at = CASE WHEN bound_at = '' THEN ? ELSE bound_at END, "
            "used_count = used_count + 1 "
            "WHERE code_hash = ? AND status = ? AND used_count < max_uses "
            "AND (expires_at = '' OR expires_at > ?)",
            (STATUS_BOUND, STATUS_PENDING, user_id, now, code_hash, STATUS_PENDING, now),
        )
        return cursor.rowcount == 1
