"""把遗留 owner / 大纲候选归属一次性收敛到管理员账号（PRD 3.8，Q7）。

背景
----
作者曾有两条身份线：`.env` 邀请码映射出的 `beta-user-001`（6 个项目、19 条大纲候选，
`users` 表里没有这一行）与用户名密码账号 `ec1ec540af85`（1 个项目）；另有一个已从
`users` 表消失的孤儿 owner `c7836e0fa58a`（1 个项目，除管理员外无人能访问）。
本脚本把这三处收敛到 `ec1ec540af85`，让两条路走同一个身份。

安全约定（为什么默认只预演）
----------------------------
写错 owner = 作品归属混乱，是本轮风险最高的一步。因此：

- 默认 **dry-run**：只打印"将改动多少行"，绝不写库；
- `--apply` 前**强制备份**整个数据库到 `data/backups/<db文件名>.<UTC 时间戳>.bak`，
  备份失败即中止（宁可不迁，也不能没有退路）；
- 只改 `projects.owner_id` / `outline_candidates.user_id` 的**值**，不新增、不删除任何行；
- `user_sessions` **绝对不碰**（否则作者会被踢下线）；
- 事务包裹，出错整体回滚；重复执行第二次改动 0 行（幂等）。

用法：

    python scripts/migrate_legacy_owner.py                  # 预演（默认，不写库）
    python scripts/migrate_legacy_owner.py --apply          # 真正写入（先强制备份）
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import DB_PATH  # noqa: E402


# 需要迁走的遗留身份（含已从 users 表消失的孤儿 owner）
FROM_OWNER_IDS = ("beta-user-001", "c7836e0fa58a")
# 收敛目标：作者本人的用户名密码账号
TO_OWNER_ID = "ec1ec540af85"

# 只允许改这两张表的归属列；user_sessions 刻意不在其中
TARGETS = (("projects", "owner_id"), ("outline_candidates", "user_id"))


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def plan_changes(
    conn: sqlite3.Connection,
    from_ids: tuple[str, ...] = FROM_OWNER_IDS,
    to_id: str = TO_OWNER_ID,
) -> dict[str, int]:
    """只读预演：每张表将改动多少行（已被迁走的行不再计入 → 天然幂等）。"""
    plan: dict[str, int] = {}
    for table, column in TARGETS:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({_placeholders(from_ids)})",
            tuple(from_ids),
        ).fetchone()
        plan[f"{table}.{column}"] = int(row[0]) if row else 0
    return plan


def apply_changes(
    conn: sqlite3.Connection,
    from_ids: tuple[str, ...] = FROM_OWNER_IDS,
    to_id: str = TO_OWNER_ID,
) -> dict[str, int]:
    """按计划写入，事务包裹；返回每张表**实际**改动的行数。

    只 UPDATE 归属列的值，没有任何 INSERT / DELETE。
    """
    changed: dict[str, int] = {}
    with conn:  # 出异常自动回滚
        for table, column in TARGETS:
            cursor = conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} IN ({_placeholders(from_ids)})",
                (to_id, *from_ids),
            )
            changed[f"{table}.{column}"] = int(cursor.rowcount)
    return changed


def count_sessions(conn: sqlite3.Connection) -> int:
    """`user_sessions` 行数（迁移前后必须一致：不动它就是不动作者的登录态）。"""
    row = conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()
    return int(row[0]) if row else 0


def backup_database(
    db_path: Path,
    *,
    backup_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """把整个库文件复制为 `<db文件名>.<UTC 时间戳>.bak`；复制失败即抛异常。

    默认落在库文件同级的 `backups/` 目录（即 PRD 说的 `data/backups/`）。
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    target_dir = backup_dir or (db_path.parent / "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    archive = target_dir / f"{db_path.name}.{stamp}.bak"
    # 绝不覆盖已有备份：同一秒内重复 --apply 时，第一次的备份才是"迁移前"状态，
    # 被第二次覆盖就等于把退路弄丢了。宁可加序号。
    suffix = 1
    while archive.exists():
        archive = target_dir / f"{db_path.name}.{stamp}-{suffix}.bak"
        suffix += 1
    shutil.copy2(db_path, archive)
    if not archive.is_file() or archive.stat().st_size == 0:
        raise RuntimeError(f"备份失败（文件为空或不存在）：{archive}")
    return archive


def _print_plan(plan: dict[str, int]) -> int:
    total = 0
    print("将改动行数：")
    for key, count in plan.items():
        print(f"  {key:<28} {count}")
        total += count
    if total == 0:
        print("  （无——已经是最新状态，重复执行安全）")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_legacy_owner.py",
        description="把遗留 owner / 大纲候选 user_id 一次性收敛到管理员账号（默认只预演，不写库）",
    )
    parser.add_argument("--apply", action="store_true", help="真正写库；写入前强制备份数据库文件")
    parser.add_argument("--db", type=Path, default=None, help="数据库文件路径（默认用后端配置解析出的库）")
    parser.add_argument(
        "--from",
        dest="from_ids",
        default=",".join(FROM_OWNER_IDS),
        help="要迁走的遗留 owner，逗号分隔（默认 beta-user-001,c7836e0fa58a）",
    )
    parser.add_argument("--to", dest="to_id", default=TO_OWNER_ID, help=f"迁移目标账号 id（默认 {TO_OWNER_ID}）")
    args = parser.parse_args(argv)

    db_path = (args.db or DB_PATH).expanduser().resolve()
    from_ids = tuple(item.strip() for item in args.from_ids.split(",") if item.strip())
    if not from_ids:
        print("参数错误：--from 不能为空。")
        return 2
    if args.to_id in from_ids:
        print("参数错误：--to 不能出现在 --from 里。")
        return 2

    print(f"目标数据库：{db_path}")
    print(f"归属迁移：{', '.join(from_ids)}  ->  {args.to_id}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        plan = plan_changes(conn, from_ids, args.to_id)
        total = _print_plan(plan)
        sessions_before = count_sessions(conn)
        if not args.apply:
            print()
            print("[预演] 未写库。确认上面的数字无误后，加 --apply 执行（会先备份）。")
            return 0
        print()
        archive = backup_database(db_path)
        print(f"[备份] {archive}")
        changed = apply_changes(conn, from_ids, args.to_id)
        print("[写入] 实际改动行数：")
        for key, count in changed.items():
            print(f"  {key:<28} {count}")
        sessions_after = count_sessions(conn)
        print(f"{'OK' if sessions_after == sessions_before else '异常'} user_sessions 未改动：{sessions_before} -> {sessions_after}")
        remaining = plan_changes(conn, from_ids, args.to_id)
        left = sum(remaining.values())
        print(f"复查：仍指向遗留 owner 的行数 = {left}（应为 0）")
        if left:
            print("警告：仍有残留，请人工复核后再决定是否重跑。")
            return 1
        print("完成。再跑一次 --apply 会显示 0 行（幂等）。")
        return 0
    except Exception as exc:  # noqa: BLE001  事务已回滚，这里只负责把原因讲清楚
        print(f"迁移失败并已回滚：{exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
