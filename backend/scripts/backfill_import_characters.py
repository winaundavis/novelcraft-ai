"""把「导入时识别到的人物」补进已有项目的 `projects.characters`。

背景
----
导入结果此前只写 `import_analysis`，没有写 `projects.characters`，而「人物」页读的是后者 ——
于是导入完一整本书，人物页里仍然只有建项目时自动生成的主角一位。

代码已修（见 `app/api/imports.py` 的 `_merge_analysis_characters`），但**已经导入过的项目**
里那份识别结果还躺在 `import_analysis` 里。本脚本把它就地补进去，不必重新导入
（重新导入会把现有章节整批回收重建，代价远大于补几条人物）。

规则与导入时完全一致（同一个函数），因此**只增不改**：
  - 同名角色一律保留作者写的那份档案，只补项目里还没有的名字；
  - 新增条目只搬事实（正文出现次数）+ 一句「启发式识别、请人工确认」，
    不编造性别/外貌/性格。

用法（默认只预览，不写入）::

    python scripts/backfill_import_characters.py                    # 预览会补哪些人
    python scripts/backfill_import_characters.py --project <id>     # 只处理一个项目
    python scripts/backfill_import_characters.py --exclude 安静,阳光  # 先把误判的挑掉
    python scripts/backfill_import_characters.py --apply            # 写入（先自动备份旧值）

写入前会把受影响项目的**旧 characters 原样**存到**该数据库旁边**的
`characters-backup-<时间戳>.json`，需要回滚时按项目 id 贴回即可。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.imports import _merge_analysis_characters  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db import get_conn  # noqa: E402


def _db_path() -> Path:
    """与 `app.core.config` 一致的解析方式（override 优先，否则 data_dir/novelcraft.db）。"""
    if settings.novelcraft_db_path:
        return Path(settings.novelcraft_db_path).expanduser().resolve()
    return Path(settings.data_dir).expanduser().resolve() / "novelcraft.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="把导入识别到的人物补进已有项目的人物表")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    parser.add_argument("--project", default=None, help="只处理指定项目 id")
    parser.add_argument(
        "--exclude",
        default="",
        help="不补入的名字（逗号分隔）。识别是词频/词性启发式，形容词、称谓名容易误判，"
        "这里相当于导入预览里的那个叉号。",
    )
    args = parser.parse_args()
    excluded = {name.strip() for name in args.exclude.split(",") if name.strip()}

    print(f"目标库: {_db_path()}")

    with get_conn() as conn:
        sql = "SELECT id, name, characters, import_analysis FROM projects"
        params: tuple = ()
        if args.project:
            sql += " WHERE id = ?"
            params = (args.project,)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("没有找到项目。")
            return 1

        plans = []
        for row in rows:
            try:
                analysis = json.loads(row["import_analysis"] or "null") or {}
            except (TypeError, ValueError):
                analysis = {}
            if not isinstance(analysis, dict) or not (analysis.get("characters") or []):
                continue
            if excluded:
                analysis = {
                    **analysis,
                    "characters": [
                        item
                        for item in analysis["characters"]
                        if not (isinstance(item, dict) and str(item.get("name") or "").strip() in excluded)
                    ],
                }
            merged_json, added = _merge_analysis_characters(row["characters"], analysis)
            if added:
                plans.append({"id": row["id"], "name": row["name"], "added": added, "value": merged_json})

        if not plans:
            print("没有需要补的项目：都已经包含导入识别到的人物（或没有导入分析）。")
            return 0

        for plan in plans:
            print(f"\n{plan['id']}  {plan['name']}")
            print(f"  会补入 {plan['added']} 位人物")
            merged = json.loads(plan["value"])
            for item in merged[-plan["added"]:]:
                print(f"    + {item['name']}")

        total = sum(plan["added"] for plan in plans)
        print(f"\n共 {len(plans)} 个项目、{total} 位人物会被补入。")

        if not args.apply:
            print("这是预览，未写入。确认无误后加 --apply。")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = _db_path().parent / f"characters-backup-{stamp}.json"
        # 同一秒内连跑两次也不能把上一份备份盖掉：备份是唯一的回滚凭据
        suffix = 2
        while backup_path.exists():
            backup_path = _db_path().parent / f"characters-backup-{stamp}-{suffix}.json"
            suffix += 1
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # 旧值：按项目 id 重新读一遍，保证备份内容与将要覆盖的完全相同
        old_values = {
            plan["id"]: conn.execute(
                "SELECT characters FROM projects WHERE id = ?", (plan["id"],)
            ).fetchone()["characters"]
            for plan in plans
        }
        with backup_path.open("w", encoding="utf-8") as handle:
            json.dump(
                [{"projectId": plan["id"], "characters": old_values[plan["id"]]} for plan in plans],
                handle,
                ensure_ascii=False,
                indent=2,
            )
        print(f"旧值已备份: {backup_path}")

        for plan in plans:
            conn.execute("UPDATE projects SET characters = ? WHERE id = ?", (plan["value"], plan["id"]))
            print(f"  已补入 {plan['id']}：{plan['added']} 位")

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
