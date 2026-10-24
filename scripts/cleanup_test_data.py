#!/usr/bin/env python3
"""
历史测试数据清理脚本 — 默认 dry-run 模式
用法:
  python scripts/cleanup_test_data.py           # dry-run，只打印将要删除的内容
  python scripts/cleanup_test_data.py --execute  # 实际执行删除
"""

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "backend" / "data" / "novelcraft.db"
BACKUP_PATH = DB_PATH.with_suffix(".db.backup")


def backup():
    """备份数据库"""
    shutil.copy2(DB_PATH, BACKUP_PATH)
    print(f"✅ 已备份: {BACKUP_PATH} ({BACKUP_PATH.stat().st_size:,} 字节)")


def list_projects(conn: sqlite3.Connection):
    """列出所有项目"""
    rows = conn.execute(
        "SELECT id, name, current_word_count, status FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    return rows


def find_candidates(conn: sqlite3.Connection) -> list[dict]:
    """找出待清理的项目"""
    rows = list_projects(conn)
    seen_names = {}
    candidates = []

    for row in rows:
        pid, name, words, status = row

        # 自动化测试项目
        if name.startswith("automation-"):
            candidates.append({"id": pid, "name": name, "words": words, "reason": "自动化测试残留"})
            continue

        # 导入烟雾测试项目
        if name.startswith("import-"):
            candidates.append({"id": pid, "name": name, "words": words, "reason": "导入测试残留"})
            continue

        # 乱码名项目
        if any(ord(c) > 0xFFFF or c == "?" for c in name):
            candidates.append({"id": pid, "name": name, "words": words, "reason": "乱码项目名"})
            continue

        # 未命名空项目
        if name.startswith("未命名") and words == 0:
            candidates.append({"id": pid, "name": name, "words": words, "reason": "空命名项目"})
            continue

        # 重复项目（保留字数最多的）
        if name in seen_names:
            prev_pid, prev_words = seen_names[name]
            if words <= prev_words:
                candidates.append({"id": pid, "name": name, "words": words, "reason": f"重复项目(保留{prev_words}字版本)"})
            else:
                candidates.append({"id": prev_pid, "name": name, "words": prev_words, "reason": f"重复项目(保留{words}字版本)"})
                seen_names[name] = (pid, words)
            continue

        seen_names[name] = (pid, words)

    return candidates


def dry_run(conn: sqlite3.Connection):
    """打印将要删除的内容"""
    candidates = find_candidates(conn)
    if not candidates:
        print("✅ 没有需要清理的项目。")
        return

    total_chapters = 0
    print(f"\n📋 将要删除 {len(candidates)} 个项目:\n")
    print(f"{'项目名':24s} | {'字数':>6s} | {'章节':>4s} | 原因")
    print("-" * 70)

    for c in candidates:
        ch_count = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE project_id=?", (c["id"],)
        ).fetchone()[0]
        total_chapters += ch_count
        print(f"{c['name'][:24]:24s} | {c['words']:>6d} | {ch_count:>4d} | {c['reason']}")

    print("-" * 70)
    print(f"共 {len(candidates)} 个项目, {total_chapters} 个章节")
    print("\n⚠️  以上为 dry-run 模式。执行删除请加 --execute")


def execute_cleanup(conn: sqlite3.Connection):
    """执行删除"""
    candidates = find_candidates(conn)
    if not candidates:
        print("✅ 没有需要清理的项目。")
        return

    backup()

    total_chapters = 0
    for c in candidates:
        ch_count = conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE project_id=?", (c["id"],)
        ).fetchone()[0]
        conn.execute("DELETE FROM chapters WHERE project_id=?", (c["id"],))
        conn.execute("DELETE FROM projects WHERE id=?", (c["id"],))
        total_chapters += ch_count

    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    print(f"\n✅ 已清理 {len(candidates)} 个项目, {total_chapters} 个章节")
    print(f"剩余 {remaining} 个项目")


def main():
    parser = argparse.ArgumentParser(description="清理历史测试数据")
    parser.add_argument("--execute", action="store_true", help="实际执行删除（默认 dry-run）")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"❌ 数据库不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    try:
        if args.execute:
            execute_cleanup(conn)
        else:
            dry_run(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
