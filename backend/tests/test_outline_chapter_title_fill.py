"""章节名补齐（2026-10-20）。

现象：一部分章节只有「第11章」，没有章节名。
根因：模型给出的 arc `chapters` 数量偶尔少于 `chapterCount`，`_generate_outline_nodes`
把缺的尾部章节落成裸「第N章」。实测 100 章大纲里有 14 章缺名，且都集中在弧的尾部。

这里锁住：
1. 有缺口才补（无缺口零成本、零风险，不额外调 LLM）；
2. 模型补名可用时用模型的名字，且不与已有标题重复；
3. 模型失败/不可用时用「弧名·序号」确定性补名，绝不留下裸「第N章」。
"""
from __future__ import annotations

import pytest

from app.services import ai_service


class _FakeProvider:
    def __init__(self, reply: str = "", error: Exception | None = None):
        self._reply = reply
        self._error = error
        self.calls: list[dict] = []

    async def chat(self, messages, role=None, **kwargs):
        self.calls.append({"role": role, "system": messages[0]["content"], "user": messages[-1]["content"]})
        if self._error:
            raise self._error
        return self._reply


def _nodes(*titles: str, arc_title: str = "重建之路") -> list[dict]:
    arc = {
        "id": "arc-1",
        "type": "arc",
        "title": arc_title,
        "summary": "幸存者在废墟上重新建立秩序，代价是有人再也回不来。",
        "chapters": [],
    }
    chapters = [
        {"id": f"c{index}", "type": "chapter", "title": title, "arcId": "arc-1", "order": index}
        for index, title in enumerate(titles, start=1)
    ]
    return [arc, *chapters]


def _all_named(nodes: list[dict]) -> list[str]:
    bare = [
        node["title"]
        for node in nodes
        if node.get("type") == "chapter" and not ai_service.chapter_name_only(node["title"])
    ]
    return bare


@pytest.mark.asyncio
async def test_fills_missing_titles_from_model_reply():
    nodes = _nodes("第96章 断墙", "第97章 空柜", "第98章", "第99章", "第100章")
    reply = '{"titles":[{"arc":"重建之路","titles":["新灶","旧债","余火"]}]}'

    filled = await ai_service._fill_missing_chapter_titles(nodes, _FakeProvider(reply))
    titles = [node["title"] for node in filled if node.get("type") == "chapter"]

    assert titles[0] == "第96章 断墙"
    assert titles[1] == "第97章 空柜"
    assert titles[2] == "第98章 新灶"
    assert titles[3] == "第99章 旧债"
    assert titles[4] == "第100章 余火"
    assert _all_named(filled) == []


@pytest.mark.asyncio
async def test_no_gap_means_no_extra_llm_call():
    provider = _FakeProvider('{"titles":[]}')
    nodes = _nodes("第1章 断墙", "第2章 空柜")

    filled = await ai_service._fill_missing_chapter_titles(nodes, provider)

    assert provider.calls == []
    assert [node["title"] for node in filled if node.get("type") == "chapter"] == ["第1章 断墙", "第2章 空柜"]


@pytest.mark.asyncio
async def test_falls_back_to_arc_scoped_names_when_model_fails():
    nodes = _nodes("第96章 断墙", "第97章", "第98章")
    provider = _FakeProvider(error=ai_service.LLMResponseError("boom"))

    filled = await ai_service._fill_missing_chapter_titles(nodes, provider)

    assert _all_named(filled) == []
    titles = [node["title"] for node in filled if node.get("type") == "chapter"]
    # 有序号，且不与已有标题相撞
    assert titles[1] == "第97章 重建之路·2"
    assert titles[2] == "第98章 重建之路·3"


@pytest.mark.asyncio
async def test_duplicate_suggestions_are_not_applied():
    nodes = _nodes("第1章 断墙", "第2章", "第3章")
    # 模型把已有标题又给了一遍 + 重复项，只有「新灶」可用
    reply = '{"titles":[{"arc":"重建之路","titles":["断墙","新灶","新灶"]}]}'

    filled = await ai_service._fill_missing_chapter_titles(nodes, _FakeProvider(reply))
    titles = [node["title"] for node in filled if node.get("type") == "chapter"]

    assert titles[0] == "第1章 断墙"
    assert titles[1] == "第2章 新灶"
    assert titles[2] == "第3章 重建之路·3"  # 重复项被丢弃 → 确定性补名
    assert _all_named(filled) == []


@pytest.mark.asyncio
async def test_outline_generation_fills_gaps_end_to_end(monkeypatch):
    """整体链路：模型少给章节名时，generate_outline 产出的章节节点不应有裸「第N章」。"""
    volume_reply = (
        '{"mainSummary":"鬼火降临，殡葬师成为引路人。",'
        ' "volumes":[{"title":"鬼火初燃","summary":"引路人第一次送走熟人。",'
        ' "conflict":"熟人变怨魂","revelation":"鬼火认主并非偶然","emotion":"阴冷",'
        ' "arcs":[{"title":"渡口","summary":"完整小故事。","chapterCount":6,'
        ' "chapters":["未至","夜行"]}]}]}'
    )
    title_reply = '{"titles":[{"arc":"渡口","titles":["桥上","回头路","水声","灯灭"]}]}'

    class _Provider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, role=None, **kwargs):
            self.calls += 1
            return volume_reply if self.calls == 1 else title_reply

    provider = _Provider()
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _llm: provider)
    monkeypatch.setattr(ai_service, "get_llm_call_meta", lambda: {"finish_reason": "stop"})
    monkeypatch.setattr(ai_service, "set_llm_audit_context", lambda **_kwargs: None)
    monkeypatch.setattr(ai_service, "reset_llm_audit_context", lambda _token: None)
    monkeypatch.setattr(ai_service, "clear_llm_call_meta", lambda: None)

    req = ai_service.GenerateOutlineRequest(
        projectId="p-fill",
        premise="鬼火降临世界，殡葬师成为引路人",
        targetChapters=6,
        targetWordCount=20000,
    )
    result = await ai_service.generate_outline(req)

    assert _all_named(result["nodes"]) == []
    chapters = [node["title"] for node in result["nodes"] if node.get("type") == "chapter"]
    assert len(chapters) == 6
    assert all(ai_service.chapter_name_only(title) for title in chapters)
