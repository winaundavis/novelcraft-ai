"""大纲生成的章节名（2026-10-18）。

要求：每一章都要有自己的名字，且名字要进标题（左侧导航只渲染 `title`，
不带「第N章」就会丢掉序号）。这里是**纯函数级**的约束测试。
"""
from __future__ import annotations

from app.models.schemas import GenerateOutlineRequest
from app.services import ai_service


def _request(target_chapters: int = 6) -> GenerateOutlineRequest:
    return GenerateOutlineRequest(
        projectId="p1",
        premise="一个人在陌生城市追查一段被隐瞒的往事",
        targetChapters=target_chapters,
    )


def _volume(title: str, summary: str, chapter_count: int, chapter_titles: list[str]) -> dict:
    arc: dict = {"title": f"{title}·主线", "summary": summary, "chapterCount": chapter_count}
    if chapter_titles:
        arc["chapters"] = [{"title": name, "summary": f"{name} 的一句话摘要"} for name in chapter_titles]
    return {"title": title, "summary": summary, "arcs": [arc]}


def _chapter_titles(nodes: list[dict]) -> list[str]:
    return [node["title"] for node in nodes if node.get("type") == "chapter"]


def test_ai_chapter_names_are_used_and_numbered_globally():
    nodes = ai_service._generate_outline_nodes(
        _request(6),
        "reply",
        volume_plans=[
            _volume("第一卷", "起点", 3, ["雨夜来客", "门后的脚步声", "灰烬"]),
            _volume("第二卷", "追踪", 3, ["铜铃", "旧照片", "回响"]),
        ],
    )
    # 6 章分两卷各 3 章：编号必须是**全书全局**的 1..6，不是每卷从 1 重来
    assert _chapter_titles(nodes) == [
        "第1章 雨夜来客",
        "第2章 门后的脚步声",
        "第3章 灰烬",
        "第4章 铜铃",
        "第5章 旧照片",
        "第6章 回响",
    ]


def test_model_that_repeats_the_number_is_not_doubled():
    # 提示词已经要求"不要写第N章"，但模型偶尔不听话：
    # 必须剥掉它自己的编号再拼，否则会出现「第2章 第2章 门后的脚步声」。
    nodes = ai_service._generate_outline_nodes(
        _request(6),
        "reply",
        volume_plans=[
            _volume("第一卷", "起点", 3, ["雨夜来客", "第2章 门后的脚步声", "第3章：灰烬"]),
            _volume("第二卷", "追踪", 3, ["铜铃", "旧照片", "回响"]),
        ],
    )
    assert _chapter_titles(nodes)[:3] == ["第1章 雨夜来客", "第2章 门后的脚步声", "第3章 灰烬"]


def test_missing_ai_names_fall_back_to_plain_numbers():
    """老形态（没有 chapters 字段）必须与改动前完全一致 —— 存量项目不受影响。"""
    nodes = ai_service._generate_outline_nodes(
        _request(6),
        "reply",
        volume_plans=[
            _volume("第一卷", "起点", 3, []),
            _volume("第二卷", "追踪", 3, []),
        ],
    )
    assert _chapter_titles(nodes) == [f"第{index}章" for index in range(1, 7)]


def test_arc_chapter_titles_tolerates_loose_shapes():
    assert ai_service._arc_chapter_titles(None) == []
    assert ai_service._arc_chapter_titles("不是列表") == []
    assert ai_service._arc_chapter_titles(["雨夜来客", {"title": "灰烬"}, {"summary": "没有标题"}, ""]) == [
        "雨夜来客",
        "灰烬",
    ]
