"""把「导入产生的大纲节点 id」重连回它本来就该指向的那一行章节。

背景
----
导入接口此前用 `gen_id()` 生成大纲节点 id，而章节行用的是扫描阶段的
`chapter["id"]` —— 两套 id 从写入那一刻起就对不上。章节正文完好无损，
但作者点开导航里的任意一章，前端 `GET /chapters/{node.id}` 拿到 404，
正文一片空白（实测 `f0ee954f3291`：114 章全部有正文，114 个节点 id 命中数为 0）。

接口已修（见 `app/api/imports.py` 的 `_build_import_outline`），**以后的新导入不会再错**；
但此前已落库的大纲仍是错的，只能就地修一次。

本脚本只做一件事：改写节点的 `id`，让它指向真实存在的章节行。

匹配规则（保守：拿不准就不动，只报告）
--------------------------------------
1. 节点 id 已经是本项目真实章节 id           -> 不动
2. 用节点的 `order` 找到唯一一行章节，且标题完全一致 -> 改写 id
3. order 上有多行、标题对不上、order 找不到人、
   或目标章节已被别的节点占用               -> 只报告，不写入

只改 id。标题、顺序、摘要、正文一律不碰，也不新增或删除任何节点。

用法（默认只预览，不写入）::

    python scripts/repair_import_outline_ids.py                    # 预览全部项目
    python scripts/repair_import_outline_ids.py --project <id>     # 只处理一个项目
    python scripts/repair_import_outline_ids.py --apply            # 写入（先自动备份旧值）

写入前会把受影响项目的**旧 outline 原样**存到**该数据库旁边**的
`outline-ids-backup-<时间戳>.json`，需要回滚时按项目 id 贴回即可。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db import get_conn  # noqa: E402


def _db_path() -> Path:
    """与 `app.core.config` 一致的解析方式（override 优先，否则 data_dir/novelcraft.db）。"""
    if settings.novelcraft_db_path:
        return Path(settings.novelcraft_db_path).expanduser().resolve()
    return Path(settings.data_dir).expanduser().resolve() / "novelcraft.db"


def _db_label() -> str:
    return str(_db_path())


def _load_projects(conn, project_filter: str | None) -> list:
    sql = "SELECT id, name, outline FROM projects"
    params: tuple = ()
    if project_filter:
        sql += " WHERE id = ?"
        params = (project_filter,)
    return conn.execute(sql, params).fetchall()


def _load_chapters(conn, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, chapter_order FROM chapters WHERE project_id = ? "
        "ORDER BY chapter_order ASC, created_at ASC",
        (project_id,),
    ).fetchall()
    return [
        {"id": row["id"], "title": (row["title"] or "").strip(), "order": row["chapter_order"]}
        for row in rows
    ]


def _plan_project(project_row, chapters: list[dict]) -> dict:
    """算出一个项目要改哪些节点 id；不改任何数据，纯计算。"""
    raw = project_row["outline"]
    try:
        parsed = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        parsed = None

    plan = {
        "id": project_row["id"],
        "name": project_row["name"],
        "nodeCount": 0,
        "chapters": len(chapters),
        "fixes": [],       # [(node_title, old_id, new_id)]
        "already_ok": 0,
        "orphan": [],      # 找不到对应章节的节点标题
        "conflict": [],    # 找到了但不敢改（标题不符 / 歧义 / 已被占用）
        "outline": parsed,
    }
    if not isinstance(parsed, dict):
        return plan

    nodes = parsed.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return plan
    plan["nodeCount"] = len(nodes)

    chapter_ids = {item["id"] for item in chapters}
    by_order: dict[int, list[dict]] = {}
    for item in chapters:
        by_order.setdefault(item["order"], []).append(item)
    by_title: dict[str, list[dict]] = {}
    for item in chapters:
        by_title.setdefault(item["title"], []).append(item)

    # 已经被合法节点占用的章节：避免两个节点改到同一章
    taken = {node.get("id") for node in nodes if isinstance(node, dict) and node.get("id") in chapter_ids}

    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        if node.get("type") != "chapter":
            # 卷 / 弧等结构节点没有对应章节，不在本次修复范围内
            continue
        node_id = node.get("id")
        title = (node.get("title") or "").strip()
        if node_id in chapter_ids:
            plan["already_ok"] += 1
            continue

        target = None
        reason = ""
        same_order = by_order.get(node.get("order"), [])
        if len(same_order) == 1:
            candidate = same_order[0]
            if candidate["title"] == title:
                target = candidate
            else:
                reason = f"order={node.get('order')} 上的章节标题是「{candidate['title']}」，与节点「{title}」不符"
        elif len(same_order) > 1:
            titled = [item for item in same_order if item["title"] == title]
            if len(titled) == 1:
                target = titled[0]
            else:
                reason = f"order={node.get('order')} 上有 {len(same_order)} 行章节，无法唯一定位"
        else:
            titled = by_title.get(title, [])
            if len(titled) == 1:
                target = titled[0]
                if target["order"] != node.get("order"):
                    reason = f"按标题匹配到第 {target['order']} 章（节点记的是 order={node.get('order')}）"
            else:
                reason = f"本项目没有 order={node.get('order')} 的章节"

        if target is None:
            plan["orphan"].append({"index": index, "title": title, "reason": reason})
            continue
        if target["id"] in taken:
            plan["conflict"].append({"index": index, "title": title, "reason": f"章节 {target['id']} 已被其它节点占用"})
            continue
        taken.add(target["id"])
        plan["fixes"].append({"index": index, "title": title, "oldId": node_id, "newId": target["id"], "note": reason})

    return plan


def _describe(plan: dict) -> None:
    print(f"\n{plan['id']}  {plan['name']}")
    print(f"  章节 {plan['chapters']} 行 / 大纲节点 {plan['nodeCount']} 个")
    if not plan["nodeCount"]:
        print("  无大纲节点，跳过。")
        return
    print(f"  已正确 {plan['already_ok']} 个 / 待修复 {len(plan['fixes'])} 个 / "
          f"无法定位 {len(plan['orphan'])} 个 / 不敢改 {len(plan['conflict'])} 个")
    for item in plan["fixes"][:5]:
        note = f"（{item['note']}）" if item["note"] else ""
        print(f"    ~ 「{item['title']}」{item['oldId']} -> {item['newId']}{note}")
    if len(plan["fixes"]) > 5:
        print(f"    …… 另有 {len(plan['fixes']) - 5} 项")
    for item in plan["orphan"][:5]:
        print(f"    ? 无法定位：「{item['title']}」{item['reason']}")
    if len(plan["orphan"]) > 5:
        print(f"    …… 另有 {len(plan['orphan']) - 5} 项无法定位")
    for item in plan["conflict"][:5]:
        print(f"    ! 跳过：「{item['title']}」{item['reason']}")
    if len(plan["conflict"]) > 5:
        print(f"    …… 另有 {len(plan['conflict']) - 5} 项被跳过")


def main() -> int:
    parser = argparse.ArgumentParser(description="把导入产生的大纲节点 id 重连回真实章节行")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    parser.add_argument("--project", default=None, help="只处理指定项目 id")
    args = parser.parse_args()

    print(f"目标库: {_db_label()}")

    with get_conn() as conn:
        rows = _load_projects(conn, args.project)
        if not rows:
            print("没有找到项目。")
            return 1

        plans = []
        for row in rows:
            plan = _plan_project(row, _load_chapters(conn, row["id"]))
            if plan["nodeCount"]:
                plans.append(plan)

        if not plans:
            print("没有任何项目带大纲节点，无需处理。")
            return 0

        for plan in plans:
            _describe(plan)

        targets = [plan for plan in plans if plan["fixes"]]
        if not targets:
            print("\n没有需要修复的节点 id。")
            return 0

        total = sum(len(plan["fixes"]) for plan in targets)
        print(f"\n共 {len(targets)} 个项目、{total} 个节点 id 会被改写。")

        if not args.apply:
            print("这是预览，未写入。确认无误后加 --apply。")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # 备份放在**真实库旁边**（与仓库里 `beta-user-001.before-*.db` 的习惯一致）。
        # 不用 data_dir：它是相对路径，会随着在哪个目录下执行而漂到别处，
        # 让人以为「备份过了」却在库里找不到。
        backup_path = _db_path().parent / f"outline-ids-backup-{stamp}.json"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("w", encoding="utf-8") as handle:
            json.dump(
                [{"projectId": plan["id"], "outline": plan["outline"]} for plan in targets],
                handle,
                ensure_ascii=False,
                indent=2,
            )
        print(f"旧值已备份: {backup_path}")

        for plan in targets:
            for item in plan["fixes"]:
                plan["outline"]["nodes"][item["index"]]["id"] = item["newId"]
            conn.execute(
                "UPDATE projects SET outline = ? WHERE id = ?",
                (json.dumps(plan["outline"], ensure_ascii=False), plan["id"]),
            )
            print(f"  已写入 {plan['id']}：改写 {len(plan['fixes'])} 个节点 id")

    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
