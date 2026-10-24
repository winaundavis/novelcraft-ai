"""大纲节点 id 修复脚本的匹配规则（`scripts/repair_import_outline_ids.py`）。

背景：导入接口此前给大纲节点生成的 id 与 `chapters` 行的主键不是同一个，
于是章节导航指向不存在的章节 —— 正文完好却点开全空白（实测 `f0ee954f3291`：
114 章全部有正文、114 个节点 id 命中数为 0）。代码已修（新导入不会再错），
而**历史数据**只能靠这个脚本重连。

脚本是唯一会改写作者已有数据的工具，所以这里把「敢改什么、不敢改什么」钉死：
按 order + 标题一致才改；对不上、有二义、已被占用一律只报告；
只动 `type == "chapter"` 的节点，且只改 id。
"""

from __future__ import annotations

import json

from scripts.repair_import_outline_ids import _plan_project


def _project(nodes: list[dict]) -> dict:
    outline = {"mainSummary": "", "structure": "three_act", "nodes": nodes, "outlineVersion": 1}
    return {"id": "p1", "name": "测试项目", "outline": json.dumps(outline, ensure_ascii=False)}


def _chapter_node(node_id: str, title: str, order: int) -> dict:
    return {
        "id": node_id,
        "parentId": None,
        "type": "chapter",
        "title": title,
        "summary": "",
        "order": order,
    }


def _chapter(chapter_id: str, title: str, order: int) -> dict:
    return {"id": chapter_id, "title": title, "order": order}


def test_nodes_that_already_point_at_real_chapters_are_left_alone():
    chapters = [_chapter("c1", "第一章 雨夜", 1), _chapter("c2", "第二章 旧档", 2)]
    plan = _plan_project(_project([_chapter_node("c1", "第一章 雨夜", 1)]), chapters)

    assert plan["fixes"] == []
    assert plan["already_ok"] == 1
    assert plan["orphan"] == []


def test_a_node_is_reconnected_when_order_and_title_agree():
    chapters = [_chapter("c1", "第一章 雨夜", 1), _chapter("c2", "第二章 旧档", 2)]
    plan = _plan_project(
        _project([_chapter_node("stale-1", "第一章 雨夜", 1), _chapter_node("stale-2", "第二章 旧档", 2)]),
        chapters,
    )

    assert [(item["oldId"], item["newId"]) for item in plan["fixes"]] == [
        ("stale-1", "c1"),
        ("stale-2", "c2"),
    ]
    # 命中得干净利落：不需要靠标题兜底去猜
    assert all(item["note"] == "" for item in plan["fixes"])
    assert plan["orphan"] == []
    assert plan["conflict"] == []


def test_a_title_mismatch_on_the_same_order_is_reported_instead_of_rewritten():
    # order=1 上坐着的不是「第一章 雨夜」而是别的章节：宁可不改，也不能把正文接错
    chapters = [_chapter("c1", "第一章 旧档", 1)]
    plan = _plan_project(_project([_chapter_node("stale-1", "第一章 雨夜", 1)]), chapters)

    assert plan["fixes"] == []
    assert len(plan["orphan"]) == 1
    assert "标题" in plan["orphan"][0]["reason"]


def test_a_duplicated_order_still_uses_the_exact_title_match():
    # 同一 order 上有多行时，标题完全一致仍然是可用证据（同一 order 里它已经唯一）
    chapters = [_chapter("c1", "第一章 雨夜", 1), _chapter("c2", "第一章 雨夜（修订）", 1)]
    plan = _plan_project(_project([_chapter_node("stale-1", "第一章 雨夜", 1)]), chapters)

    assert [item["newId"] for item in plan["fixes"]] == ["c1"]
    assert plan["orphan"] == []


def test_duplicated_orders_with_the_same_title_are_left_alone():
    # 两条候选一样好（连标题都相同）时不许挑一个：只能报告，由人来判断
    chapters = [_chapter("c1", "第一章 雨夜", 1), _chapter("c2", "第一章 雨夜", 1)]
    plan = _plan_project(_project([_chapter_node("stale-1", "第一章 雨夜", 1)]), chapters)

    assert plan["fixes"] == []
    assert len(plan["orphan"]) == 1
    assert "无法唯一定位" in plan["orphan"][0]["reason"]


def test_a_missing_order_falls_back_to_a_unique_title_match():
    # 章节被重排过：order 对不上，但标题在全项目里唯一 —— 可以接上，并在报告里注明
    chapters = [_chapter("c1", "第一章 雨夜", 40)]
    plan = _plan_project(_project([_chapter_node("stale-1", "第一章 雨夜", 1)]), chapters)

    assert [item["newId"] for item in plan["fixes"]] == ["c1"]
    assert "按标题匹配到第 40 章" in plan["fixes"][0]["note"]


def test_a_title_that_matches_nothing_is_reported():
    plan = _plan_project(_project([_chapter_node("stale-1", "第一百章 不存在的", 100)]), [])

    assert plan["fixes"] == []
    assert len(plan["orphan"]) == 1
    assert "没有 order=100 的章节" in plan["orphan"][0]["reason"]


def test_two_nodes_never_get_pointed_at_the_same_chapter():
    chapters = [_chapter("c1", "第一章 雨夜", 1)]
    plan = _plan_project(
        _project([_chapter_node("stale-1", "第一章 雨夜", 1), _chapter_node("stale-2", "第一章 雨夜", 1)]),
        chapters,
    )

    # 只接第一条；第二条既没有独立章节可指，就不能再指到 c1 上
    assert [item["newId"] for item in plan["fixes"]] == ["c1"]
    assert len(plan["conflict"]) == 1
    assert "已被其它节点占用" in plan["conflict"][0]["reason"]


def test_volume_structure_nodes_are_out_of_scope():
    volume = {"id": "v1", "parentId": None, "type": "volume", "title": "第一卷", "order": 1}
    chapters = [_chapter("c1", "第一章 雨夜", 1)]
    plan = _plan_project(_project([volume, _chapter_node("stale-1", "第一章 雨夜", 1)]), chapters)

    assert [item["newId"] for item in plan["fixes"]] == ["c1"]
    # 卷节点的 id 不是章节，不在本次修复范围（既不改，也不报成孤儿）
    assert plan["orphan"] == []
    assert all(item["title"] == "第一章 雨夜" for item in plan["fixes"])


def test_projects_without_an_outline_are_skipped_entirely():
    assert _plan_project({"id": "p1", "name": "空项目", "outline": None}, [])["nodeCount"] == 0
    assert _plan_project({"id": "p1", "name": "坏数据", "outline": "{not json"}, [])["nodeCount"] == 0


def test_planning_does_not_mutate_the_stored_outline():
    """预览必须是无副作用的 —— 看到「未写入」就不能有任何改动。"""
    chapters = [_chapter("c1", "第一章 雨夜", 1)]
    project = _project([_chapter_node("stale-1", "第一章 雨夜", 1)])
    before = project["outline"]

    plan = _plan_project(project, chapters)

    assert plan["fixes"], "这条用例需要一条待修项才有意义"
    assert project["outline"] == before
    assert json.loads(project["outline"])["nodes"][0]["id"] == "stale-1"


def test_replanning_after_applying_the_fixes_is_a_no_op():
    """脚本要求幂等：跑过一次之后，再预览必须是「没有需要修复的节点 id」。"""
    chapters = [_chapter("c1", "第一章 雨夜", 1), _chapter("c2", "第二章 旧档", 2)]
    project = _project([_chapter_node("stale-1", "第一章 雨夜", 1), _chapter_node("stale-2", "第二章 旧档", 2)])

    first = _plan_project(project, chapters)
    nodes = json.loads(project["outline"])["nodes"]
    for item in first["fixes"]:
        nodes[item["index"]]["id"] = item["newId"]

    second = _plan_project(_project(nodes), chapters)

    assert second["fixes"] == []
    assert second["already_ok"] == 2
