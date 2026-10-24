"""为「已有章节但没有大纲」的项目补生成大纲。

背景：导入接口此前把 `projects.outline` 固定写成 "null"，只落 chapters，
于是导入上百章之后「章节导航」依然一片空白。新导入已修好，但**历史项目**仍是空的。

本脚本按章节顺序补一层 `chapter` 节点（不虚构卷/弧/摘要，只搬运已有事实），
与导入时的生成逻辑同源，因此补出来的结构和以后重新导入完全一致。

用法（默认只预览，不写入）::

    python scripts/backfill_import_outline.py                 # 预览会改什么
    python scripts/backfill_import_outline.py --apply         # 真正写入
    python scripts/backfill_import_outline.py --apply --project <id>

注意：写入 target 由 `NOVELCRAFT_DB_PATH` / `data_dir` 决定，运行前先确认指向哪个库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.imports import _build_import_outline  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db import get_conn  # noqa: E402


def _chapters_without_outline(conn, project_id_filter: str | None) -> list[dict]:
    sql = (
        "SELECT p.id, p.name, p.outline, "
        "(SELECT COUNT(*) FROM chapters c WHERE c.project_id = p.id) AS chapter_count "
        "FROM projects p "
    )
    params: tuple = ()
    if project_id_filter:
        sql += "WHERE p.id = ?"
        params = (project_id_filter,)

    candidates = []
    for row in conn.execute(sql, params):
        if row["chapter_count"] == 0:
            continue
        raw = row["outline"]
        try:
            parsed = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            parsed = None
        existing_nodes = (parsed or {}).get("nodes") or []
        if existing_nodes:
            # 已有大纲的项目不动：作者可能手工调过，不该被覆盖。
            continue
        candidates.append({"id": row["id"], "name": row["name"], "chapterCount": row["chapter_count"]})
    return candidates


def _load_chapters(conn, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT title, chapter_order, word_count FROM chapters "
        "WHERE project_id = ? ORDER BY chapter_order ASC, created_at ASC",
        (project_id,),
    ).fetchall()
    return [
        {"title": row["title"], "order": row["chapter_order"], "wordCount": row["word_count"]}
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="为已有章节但缺大纲的项目补生成大纲")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    parser.add_argument("--project", default=None, help="只处理指定项目 id")
    args = parser.parse_args()

    print(f"目标库: {settings.novelcraft_db_path or Path(settings.data_dir) / 'novelcraft.db'}")

    with get_conn() as conn:
        candidates = _chapters_without_outline(conn, args.project)
        if not candidates:
            print("没有需要处理的项：所有有章节的项目都已有大纲。")
            return 0

        print(f"待处理 {len(candidates)} 个项目：")
        for item in candidates:
            print(f"  - {item['id']}  {item['name']}  {item['chapterCount']} 章")

        if not args.apply:
            print("\n这是预览（未写入）。加 --apply 才会真正补写大纲。")
            return 0

        for item in candidates:
            chapters = _load_chapters(conn, item["id"])
            outline_json = _build_import_outline(chapters, "")
            conn.execute("UPDATE projects SET outline = ? WHERE id = ?", (outline_json, item["id"]))
            print(f"  已补写 {item['id']}：{len(chapters)} 个章节节点")

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
