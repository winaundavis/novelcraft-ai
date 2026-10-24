"""长跑后记忆系统验证：统计某项目的记忆数据是否真实生成。

用法: python scripts/audit_longrun_memory.py --project <id>
输出:
  - 章/字数/fallback
  - FTS 索引行数（应≈有正文章节数）
  - 事实账本数 / 状态分布
  - 实体/关系数
  - 召回日志条数（应≈生成章数×块数）
  - 重写审计条数
  - 卷摘要数
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app.db import get_conn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="项目 ID")
    args = parser.parse_args()
    pid = args.project

    with get_conn() as conn:
        print(f"=== 项目 {pid} 记忆系统统计 ===")
        for table, label in [
            ("chapters", "章节数"),
            ("chapter_fts", "FTS 索引行数"),
            ("novel_facts", "事实账本数"),
            ("memory_entities", "实体数"),
            ("memory_relations", "关系数"),
            ("memory_recall_log", "召回日志条数"),
            ("chapter_rewrites", "重写审计条数"),
            ("volumes", "卷摘要数"),
        ]:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (pid,)).fetchone()[0]
                print(f"  {label}: {n}")
            except Exception as e:
                print(f"  {label}: 查询失败 {e}")

        # 字数
        row = conn.execute("SELECT current_word_count FROM projects WHERE id=?", (pid,)).fetchone()
        print(f"  项目字数: {row[0] if row else 'N/A'}")

        # 事实状态分布
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM novel_facts WHERE project_id=? GROUP BY status", (pid,)
            ).fetchall()
            print(f"  事实状态分布: {dict(rows) if rows else '无'}")
        except Exception as e:
            print(f"  事实状态分布: 查询失败 {e}")

        # 召回日志按块类型
        try:
            rows = conn.execute(
                "SELECT block_key, COUNT(*) FROM memory_recall_log WHERE project_id=? GROUP BY block_key", (pid,)
            ).fetchall()
            print(f"  召回日志分块: {dict(rows) if rows else '无'}")
        except Exception as e:
            print(f"  召回日志分块: 查询失败 {e}")


if __name__ == "__main__":
    main()
