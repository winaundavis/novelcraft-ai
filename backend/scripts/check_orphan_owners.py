"""孤儿 owner 体检报告（PRD 3.8 孤儿根治 / 验收 16）。

"孤儿 owner" = `projects.owner_id` 既**不在 `users` 表里**、也**不在管理员白名单
（`ADMIN_USER_IDS`）**里的项目。这类作品除管理员外无人能访问，历史上由那个无校验的
`delete_user()` 造成（已删除，见 `app/dao/users.py`）。

本脚本**只报告、绝不修改**：自动改归属比孤儿更危险（可能把别人的作品塞给你的账号）。
真要收敛，先备份，再用 `migrate_legacy_owner.py` 并人工确认行数。

用法：

    python scripts/check_orphan_owners.py                 # 用后端配置解析出的库
    python scripts/check_orphan_owners.py --db <path>     # 指定库文件
    python scripts/check_orphan_owners.py --admin-ids a,b # 覆盖管理员白名单

退出码：0 = 无孤儿；1 = 发现孤儿（便于当作交付前检查项的闸门）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db import DB_PATH  # noqa: E402


def parse_admin_ids(value: str) -> list[str]:
    """把 `ADMIN_USER_IDS` 那种逗号分隔串解析成列表（去空白、去空项、保序去重）。"""
    seen: list[str] = []
    for item in (value or "").split(","):
        token = item.strip()
        if token and token not in seen:
            seen.append(token)
    return seen


def find_orphan_owners(conn: sqlite3.Connection, allowed_ids: list[str] | None = None) -> list[dict]:
    """只读扫描：返回 [{'project_id', 'project_name', 'owner_id'}, ...]。"""
    whitelist = [item for item in (allowed_ids or []) if item]
    sql = (
        "SELECT p.id AS project_id, p.name AS project_name, p.owner_id AS owner_id"
        " FROM projects p LEFT JOIN users u ON u.id = p.owner_id"
        " WHERE u.id IS NULL"
    )
    params: tuple[str, ...] = ()
    if whitelist:
        sql += f" AND p.owner_id NOT IN ({', '.join('?' for _ in whitelist)})"
        params = tuple(whitelist)
    sql += " ORDER BY p.owner_id, p.id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _query_orphans(conn: sqlite3.Connection, table: str, column: str, allowed_ids: list[str]) -> list[dict]:
    """单表体检：该表的归属列既无 `users` 行、也不在管理员白名单里的行。

    表不存在（老库）时返回空列表，不让体检整个崩掉。
    """
    whitelist = [item for item in allowed_ids if item]
    sql = (
        f"SELECT t.id AS row_id, t.{column} AS owner_id FROM {table} t"
        f" LEFT JOIN users u ON u.id = t.{column}"
        " WHERE u.id IS NULL"
    )
    params: tuple[str, ...] = ()
    if whitelist:
        sql += f" AND t.{column} NOT IN ({', '.join('?' for _ in whitelist)})"
        params = tuple(whitelist)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def find_dangling_invites(conn: sqlite3.Connection) -> list[dict]:
    """`bound` 却指向一个不存在的账号的邀请码。

    这类码既登不进、也重新注册不了，是一把**永远打不开任何门的死钥匙**。
    它们不会出现在 `projects` 的扫描里，所以必须单独查。
    """
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT i.id AS row_id, i.code_hint, i.status, i.bound_user_id AS owner_id"
                " FROM invites i LEFT JOIN users u ON u.id = i.bound_user_id"
                " WHERE i.bound_user_id != '' AND u.id IS NULL"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return []


def find_all_orphans(conn: sqlite3.Connection, allowed_ids: list[str] | None = None) -> dict[str, list[dict]]:
    """多表体检（只返回**非空**的分类）。

    只看 `projects` 会漏掉整类孤儿：本项目历史上已经漏过两次
    （`reference_works` 与 `outline_candidates` 都是用户级资产）。
    """
    whitelist = [item for item in (allowed_ids or []) if item]
    reports: dict[str, list[dict]] = {
        "projects": find_orphan_owners(conn, whitelist),
        "reference_works": _query_orphans(conn, "reference_works", "owner_id", whitelist),
        "outline_candidates": _query_orphans(conn, "outline_candidates", "user_id", whitelist),
        "invites(dangling bound)": find_dangling_invites(conn),
    }
    return {name: rows for name, rows in reports.items() if rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_orphan_owners.py",
        description=(
            "多表扫描孤儿归属（projects.owner_id / reference_works.owner_id /"
            " outline_candidates.user_id / 悬空的邀请码绑定）；只报告不修改"
        ),
    )
    parser.add_argument("--db", type=Path, default=None, help="数据库文件路径（默认用后端配置解析出的库）")
    parser.add_argument(
        "--admin-ids",
        default=settings.admin_user_ids,
        help="管理员白名单，逗号分隔（默认取配置 ADMIN_USER_IDS）",
    )
    args = parser.parse_args(argv)

    db_path = (args.db or DB_PATH).expanduser().resolve()
    admin_ids = parse_admin_ids(args.admin_ids)
    print(f"数据库：{db_path}")
    print(f"管理员白名单（不算孤儿）：{', '.join(admin_ids) if admin_ids else '（空）'}")
    if not db_path.is_file():
        print("找不到数据库文件，无法体检。")
        return 1
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        orphans = find_orphan_owners(conn, admin_ids)
        reports = find_all_orphans(conn, admin_ids)
    finally:
        conn.close()
    if not orphans and not reports:
        print("无孤儿：项目的 owner、素材归属、大纲候选归属都有对应账号（或落在管理员白名单内），")
        print("        且没有指向已消失账号的邀请码绑定。")
        return 0
    if orphans:
        print(f"发现 {len(orphans)} 个孤儿 owner 的项目（本脚本不会修改任何数据）：")
        print(f"  {'project_id':<16} {'owner_id':<20} project_name")
        for item in orphans:
            print(f"  {str(item['project_id']):<16} {str(item['owner_id']):<20} {item['project_name']}")
    for name, rows in reports.items():
        if name == "projects":
            continue
        print()
        print(f"另外在 `{name}` 里发现 {len(rows)} 条孤儿（同一类的漏检：只看 projects 是看不见的）：")
        for item in rows:
            hint = item.get("code_hint") or ""
            suffix = f" hint={hint} status={item.get('status')}" if hint else ""
            print(f"  {str(item['row_id']):<16} owner={str(item['owner_id']):<20}{suffix}")
    print()
    print("建议：先停后端并备份数据库，再人工确认归属后用 migrate_legacy_owner.py 收敛；")
    print("      悬空的邀请码绑定可在停后端后用 SQL 手工清理（本脚本刻意不自动修）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
