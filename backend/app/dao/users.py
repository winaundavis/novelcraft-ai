from __future__ import annotations
import hashlib, hmac, os, secrets, sqlite3
from datetime import datetime, timedelta, timezone
from app.db import get_conn
from app.models.schemas import gen_id

ITERATIONS = 240_000
SESSION_DAYS = 14

def _now(): return datetime.now(timezone.utc)
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}'
def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, digest = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return scheme == 'pbkdf2_sha256' and hmac.compare_digest(actual, digest)
    except (ValueError, TypeError): return False
def create_user(username: str, password: str, is_admin=False, *, terms_version: str = '') -> str:
    uid=gen_id(); now=_now().isoformat()
    # 注册协议记录与账号同一次插入；写入失败时不会先留下账号或消耗邀请码。
    with get_conn() as c:
        c.execute(
            'INSERT INTO users(id,username,password_hash,is_admin,created_at,terms_version,terms_accepted_at) '
            'VALUES(?,?,?,?,?,?,?)',
            (uid, username, hash_password(password), int(is_admin), now, terms_version, now if terms_version else ''),
        )
    return uid
def find_user(username: str):
    """按用户名查账号（**不区分大小写**，PRD 3.3）。

    用 SQLite 的 lower() 与 `idx_users_username_lower` 函数式唯一索引保持同一口径；
    注意 SQLite 的 lower() 只折叠 ASCII，中文用户名不受影响（也不必折叠）。
    """
    with get_conn() as c:
        r=c.execute('SELECT * FROM users WHERE lower(username)=lower(?)',(username,)).fetchone()
    return dict(r) if r else None
def get_user(user_id: str):
    with get_conn() as c:
        r=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    return dict(r) if r else None


def record_terms_acceptance(user_id: str, version: str) -> None:
    """记录「用户同意了哪一版条款、什么时候同意的」。

    调用方须先核验明确同意与展示版本。仅保留最近一次接受记录，
    不表示已经阅读，也不保留历次协议接受历史。
    """
    if not user_id or not version:
        return
    with get_conn() as c:
        c.execute(
            'UPDATE users SET terms_version=?, terms_accepted_at=? WHERE id=?',
            (version, _now().isoformat(), user_id),
        )
def create_session(user_id: str):
    raw=secrets.token_urlsafe(32); now=_now(); token=hashlib.sha256(raw.encode()).hexdigest()
    with get_conn() as c: c.execute('INSERT INTO user_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)',(token,user_id,now.isoformat(),(now+timedelta(days=SESSION_DAYS)).isoformat()))
    return raw
def user_for_session(raw: str):
    if not raw: return None
    token=hashlib.sha256(raw.encode()).hexdigest()
    with get_conn() as c:
        r=c.execute('SELECT u.* FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',(token,_now().isoformat())).fetchone()
    return dict(r) if r else None
def delete_session(raw: str):
    with get_conn() as c: c.execute('DELETE FROM user_sessions WHERE token_hash=?',(hashlib.sha256((raw or '').encode()).hexdigest(),))

# 说明：这里曾有一个无校验的 `delete_user(user_id)`（只删账号与会话，不看名下作品），
# 它正是历史上出现"孤儿 owner"（projects.owner_id 指向已消失的 users 行）的原因，已删除。
# 需要注销账号请统一走 `delete_user_if_no_owned_data()`：先检查用户名下是否还有作品，
# 有则拒绝（PRD 3.8 / 15）。


def _count_trashed_chapters(conn, user_id: str) -> int:
    """统计该用户自有项目下的回收站章节数（表缺失时按 0 处理）。"""
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM chapter_trash WHERE project_id IN"
            " (SELECT id FROM projects WHERE owner_id = ?)",
            (user_id,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _count_owned_assets(conn, user_id: str) -> dict:
    """统计该用户名的**全部**归属资产。

    漏掉任何一张表，注销后就会留下一类"孤儿 owner"——本项目已经栽过两次
    （`projects` 之外的资产曾经完全没被检查）。
    """

    def _count(table: str, column: str) -> int:
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (user_id,)).fetchone()[0])
        except sqlite3.OperationalError:
            return 0  # 表不存在（老库/未迁移）时按 0 处理，不让注销整个崩掉

    return {
        "projects": _count("projects", "owner_id"),
        "trashed_chapters": _count_trashed_chapters(conn, user_id),
        # 以下是**用户级**资产：它们不随项目删除而消失，因此必须一起统计。
        "reference_works": _count("reference_works", "owner_id"),
        "outline_candidates": _count("outline_candidates", "user_id"),
    }


def owned_project_stats(user_id: str) -> dict:
    """返回该用户名下的归属资产统计（严格按归属列，管理员的全项目访问不算拥有）。"""
    with get_conn() as c:
        return _count_owned_assets(c, user_id)


def delete_user_if_no_owned_data(user_id: str) -> dict:
    """原子地注销用户，但仅当用户名下没有任何归属资产。

    在 ``BEGIN IMMEDIATE`` 写锁内先统计再删除，避免"检查通过后又创建数据"的竞态。
    成功时**同时作废该账号绑定的邀请码**——邀请码是"该账号的备用钥匙"，
    账号没了钥匙就必须一起作废，否则它会永远卡在 `bound`（既登不进、也重新注册不了）。
    返回 ``{"deleted": bool, "projects": int, "trashed_chapters": int,
    "reference_works": int, "outline_candidates": int}``；被拒绝时不删除任何行。
    """
    with get_conn() as c:
        c.execute("BEGIN IMMEDIATE")
        stats = _count_owned_assets(c, user_id)
        owned = stats["projects"] + stats["reference_works"] + stats["outline_candidates"]
        if owned:
            return {"deleted": False, **stats}
        try:
            c.execute(
                "UPDATE invites SET status = 'disabled', bound_user_id = '', bound_at = ''"
                " WHERE bound_user_id = ?",
                (user_id,),
            )
        except sqlite3.OperationalError:
            pass  # 老库还没有 invites 表：不影响注销本身
        c.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"deleted": True, **stats}
