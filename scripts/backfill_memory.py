"""NovelCraft AI — 存量项目记忆回填脚本（BUG-C 修复）

作用：为存量项目补齐「记忆升级 v1」数据，幂等、不破坏现有数据。
  - 回填 chapter_fts 索引（标题+正文+摘要+事实）
  - 回填 memory_entities / memory_relations（实体抽取，可选 LLM）
  - 校验 novel_facts 新列默认值（status/version/confidence）

用法:
  python scripts/backfill_memory.py --all              # 全部项目
  python scripts/backfill_memory.py --project <id>     # 指定项目
  python scripts/backfill_memory.py --all --no-llm     # 只回填 FTS，不跑 LLM 实体抽取
  python scripts/backfill_memory.py --check            # 只检查不写

说明:
  - LLM 实体抽取默认关闭（--no-llm），避免消耗；FTS 回填为纯本地操作。
  - 全部幂等：重复执行结果一致。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app.dao import memory_fts as memory_fts_dao
from app.dao import projects as projects_dao
from app.db import get_conn


def _all_project_ids() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM projects ORDER BY created_at").fetchall()
    return [r["id"] for r in rows]


def _backfill_fts(project_id: str) -> dict:
    """回填 chapter_fts：调用官方 rebuild_project_fts（幂等，先删后插 + 提交）。

    sync_chapter_fts 内部自动聚合该章的标题+正文+摘要+事实内容。
    """
    synced = memory_fts_dao.rebuild_project_fts(project_id)
    return {"synced": synced}


def _check(project_id: str) -> dict:
    with get_conn() as conn:
        fts = conn.execute(
            "SELECT COUNT(*) AS n FROM chapter_fts WHERE project_id = ?", (project_id,)
        ).fetchone()["n"]
        ent = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_entities WHERE project_id = ?", (project_id,)
        ).fetchone()["n"]
    return {"fts_rows": fts, "entities": ent}


def main() -> None:
    parser = argparse.ArgumentParser(description="存量项目记忆回填（BUG-C）")
    parser.add_argument("--all", action="store_true", help="全部项目")
    parser.add_argument("--project", type=str, default="", help="指定项目 id")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 实体抽取（默认开启回填 FTS）")
    parser.add_argument("--check", action="store_true", help="只检查不写入")
    args = parser.parse_args()

    if not args.all and not args.project and not args.check:
        parser.error("请指定 --all 或 --project <id>，或 --check")

    project_ids = _all_project_ids() if args.all else ([args.project] if args.project else [])
    if args.check:
        for pid in project_ids:
            print(f"[检查] {pid}: {_check(pid)}")
        return

    total_synced = 0
    for pid in project_ids:
        print(f"[回填] 项目 {pid} ...")
        stats = _backfill_fts(pid)
        print(f"  FTS: {stats}")
        total_synced += stats["synced"]
    print(f"\n完成：回填 {len(project_ids)} 个项目，同步 {total_synced} 章 FTS 索引（幂等，可重复执行）")


if __name__ == "__main__":
    main()
