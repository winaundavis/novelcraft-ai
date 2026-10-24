"""大纲卷级信号（冲突 / 揭示 / 情绪）必须逐卷不同（2026-10-20）。

用户反馈：大纲页每一卷摘要下面都是同一行小字——
「冲突：主线冲突持续升级 / 揭示：更深一层的真相开始浮出水面 / 情绪：张力上扬」。

根因两层：
1. `_generate_outline_nodes` 把这三句写死成常量，**无视**模型返回的卷级内容；
2. 提示词从未要求模型给出 conflict / revelation / emotion，分卷路径也只保留 title/summary/arcs。

这里锁住三件事：
1. 模型给了就用模型的；
2. 模型没给（或把同一句写给多卷）时，兜底文案按卷位区分，绝不重复；
3. 章节级信号按弧区分，不同弧不同句。
"""
from __future__ import annotations

import asyncio

from app.models.schemas import GenerateOutlineRequest
from app.services import ai_service

_OLD_TEMPLATES = {
    "主线冲突持续升级",
    "更深一层的真相开始浮出水面",
    "张力上扬",
}


def _request(chapters: int) -> GenerateOutlineRequest:
    return GenerateOutlineRequest(
        projectId="p-signals",
        premise="鬼火降临世界，殡葬师成为引路人",
        targetChapters=chapters,
        targetWordCount=20000,
    )


def _arc(title: str, names: list[str]) -> dict:
    return {
        "title": title,
        "summary": f"{title}的完整小故事：起点、转折与结果。",
        "chapterCount": len(names),
        "chapters": names,
    }


def _volume(title: str, signals: dict | None = None) -> dict:
    plan = {
        "title": title,
        "summary": f"{title}这一卷独有的卷末结果。",
        "arcs": [_arc(f"{title}之弧", [f"{title}标题一", f"{title}标题二", f"{title}标题三"])],
    }
    plan.update(signals or {})
    return plan


def _nodes(result: dict, node_type: str) -> list[dict]:
    return [node for node in result["nodes"] if node.get("type") == node_type]


def test_volume_signals_come_from_model_output():
    req = _request(18)  # 3 卷
    plans = [
        _volume("寒潭", {"conflict": "引路人被同行污名化", "revelation": "师父的死另有隐情", "emotion": "冷冽压抑"}),
        _volume("冥井", {"conflict": "守约者开始互相出卖", "revelation": "井底关着的并非鬼物", "emotion": "惊疑对峙"}),
        _volume("归位", {"conflict": "主角必须亲手送走至亲", "revelation": "鬼火的来历被彻底说清", "emotion": "悲壮收束"}),
    ]

    result = {"nodes": ai_service._generate_outline_nodes(req, "ignored", volume_plans=plans)}
    volumes = _nodes(result, "volume")

    assert [volume["conflict"] for volume in volumes] == [
        "引路人被同行污名化",
        "守约者开始互相出卖",
        "主角必须亲手送走至亲",
    ]
    assert volumes[1]["revelation"] == "井底关着的并非鬼物"
    assert volumes[2]["emotion"] == "悲壮收束"


def test_volume_signals_fall_back_to_distinct_text_per_volume():
    req = _request(18)

    result = {"nodes": ai_service._generate_outline_nodes(req, "只有一行兜底回复")}
    volumes = _nodes(result, "volume")

    assert len(volumes) == 3
    for key in ("conflict", "revelation", "emotion"):
        values = [volume[key] for volume in volumes]
        assert all(value.strip() for value in values), f"{key} 不能为空"
        assert len(set(values)) == 3, f"{key} 每卷都必须不同：{values}"
        assert not (_OLD_TEMPLATES & set(values)), f"{key} 不能回到旧模板句：{values}"


def test_duplicate_model_signals_are_not_repeated_across_volumes():
    req = _request(18)
    same = {"conflict": "主线冲突持续升级", "revelation": "真相浮出水面", "emotion": "张力上扬"}
    plans = [_volume("卷一", same), _volume("卷二", dict(same)), _volume("卷三", dict(same))]

    result = {"nodes": ai_service._generate_outline_nodes(req, "ignored", volume_plans=plans)}
    volumes = _nodes(result, "volume")

    for key in ("conflict", "revelation", "emotion"):
        values = [volume[key] for volume in volumes]
        assert len(set(values)) == 3, f"{key} 撞车时必须退回兜底：{values}"


def test_two_volume_outline_uses_different_signals_per_volume():
    req = _request(6)  # 2 卷

    result = {"nodes": ai_service._generate_outline_nodes(req, "只有一行兜底回复")}
    volumes = _nodes(result, "volume")

    assert len(volumes) == 2
    assert volumes[0]["conflict"] != volumes[1]["conflict"]
    assert volumes[0]["revelation"] != volumes[1]["revelation"]
    assert volumes[0]["emotion"] != volumes[1]["emotion"]


def test_chapter_signals_differ_across_arcs():
    req = _request(6)
    plan = {
        "title": "寒潭",
        "summary": "寒潭这一卷独有的卷末结果。",
        "conflict": "引路人被同行污名化",
        "revelation": "师父的死另有隐情",
        "emotion": "冷冽压抑",
        "arcs": [
            _arc("渡口", ["渡口一", "渡口二", "渡口三"]),
            _arc("夜行", ["夜行一", "夜行二", "夜行三"]),
        ],
    }
    second = dict(plan, title="冥井", summary="冥井这一卷独有的卷末结果。", arcs=[
        _arc("井底", ["井底一", "井底二", "井底三"]),
    ])

    result = {"nodes": ai_service._generate_outline_nodes(req, "ignored", volume_plans=[plan, second])}
    chapters = _nodes(result, "chapter")

    by_arc: dict[str, set[str]] = {}
    for chapter in chapters:
        by_arc.setdefault(chapter["arcId"], set()).add(chapter["conflict"])

    # 同一弧内共享本弧冲突（这是设定），不同弧必须不同
    assert all(len(values) == 1 for values in by_arc.values())
    shown = {next(iter(values)) for values in by_arc.values()}
    assert len(shown) == len(by_arc) >= 2
    assert any("渡口" in text for text in shown)
    assert any("夜行" in text for text in shown)


def test_signal_is_flattened_and_truncated():
    req = _request(18)
    long_conflict = "第一行\n\n第二行 " + "很长的冲突描述" * 20
    plans = [_volume("卷一", {"conflict": long_conflict})]

    result = {"nodes": ai_service._generate_outline_nodes(req, "ignored", volume_plans=plans)}
    conflict = _nodes(result, "volume")[0]["conflict"]

    assert "\n" not in conflict
    assert len(conflict) <= ai_service._VOLUME_SIGNAL_MAX_CHARS


def test_chunked_volume_plans_keep_volume_signals(monkeypatch):
    """分卷生成（长书路径）也必须带上卷级信号，否则组装时会退化成兜底文案。"""
    reply = (
        '{"volumes": [{"title": "鬼火初燃", "summary": "引路人第一次送走熟人。",'
        ' "conflict": "熟人变成怨魂索命", "revelation": "鬼火认主并非偶然", "emotion": "阴冷不安",'
        ' "arcs": [{"title": "第一弧", "summary": "完整小故事。", "chapterCount": 3,'
        ' "chapters": ["标题一", "标题二", "标题三"]}]}]}'
    )

    class _FakeProvider:
        async def chat(self, messages, role=None, **kwargs):
            return reply

    req = _request(6)
    plans, _summary = asyncio.run(
        ai_service._generate_outline_volume_plans(req, _FakeProvider())
    )

    assert plans[0]["conflict"] == "熟人变成怨魂索命"
    assert plans[0]["revelation"] == "鬼火认主并非偶然"
    assert plans[0]["emotion"] == "阴冷不安"
