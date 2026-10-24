"""长书大纲：首轮被截断时改走**分卷生成**（2026-10-19）。

背景：审计 `701654cc2014` —— 100 章大纲被 max_tokens 截断，`finish_reason=length`
却记为 success，半个 JSON 被解析成「每卷只有一个本卷主线弧 + 原始 JSON 当主线摘要」。

这里锁两件事：
1. 未截断时**仍然只调一次**（既有功能不被改动，也不多花钱）；
2. 截断时自动按卷拆分重来，产出完整大纲（而不是降级成模板）。
"""
from __future__ import annotations

import json

import pytest

from app.models.schemas import GenerateOutlineRequest
from app.services import ai_service


class _FakeProvider:
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, role=None, **kwargs):
        self.calls.append({
            "role": role,
            "max_tokens": kwargs.get("max_tokens"),
            "system": messages[0]["content"],
            "user": messages[-1]["content"],
        })
        return self._replies.pop(0)


class _MetaSeq:
    """按顺序返回 llm_call_meta；用完后一直返回最后一条。"""

    def __init__(self, seq: list[dict]):
        self._seq = list(seq)

    def __call__(self):
        if len(self._seq) > 1:
            return self._seq.pop(0)
        return self._seq[0]


def _request(chapters: int = 6) -> GenerateOutlineRequest:
    return GenerateOutlineRequest(
        projectId="p-chunk",
        premise="鬼火降临世界，殡葬师成为引路人",
        targetChapters=chapters,
        targetWordCount=20000,
    )


def _volume(title: str, summary: str, arc: str, names: list[str]) -> dict:
    return {
        "title": title,
        "summary": summary,
        "arcs": [{
            "title": arc,
            "summary": f"{arc}的完整小故事",
            "chapterCount": len(names),
            "chapters": names,
        }],
    }


def _install(monkeypatch, provider, meta_seq):
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _llm: provider)
    monkeypatch.setattr(ai_service, "get_llm_call_meta", _MetaSeq(meta_seq))
    monkeypatch.setattr(ai_service, "set_llm_audit_context", lambda **_kwargs: None)
    monkeypatch.setattr(ai_service, "reset_llm_audit_context", lambda _token: None)
    monkeypatch.setattr(ai_service, "clear_llm_call_meta", lambda: None)


def _chapter_titles(result: dict) -> list[str]:
    return [n["title"] for n in result["nodes"] if n.get("type") == "chapter"]


@pytest.mark.asyncio
async def test_complete_outline_still_uses_a_single_call(monkeypatch):
    """未截断 → 只调一次。既有行为与成本都不变。"""
    payload = {
        "mainSummary": "全书主线摘要",
        "volumes": [
            _volume("初火照影", "第一卷摘要", "丝线初现", ["鬼火初现", "无名女尸", "丝线牵引"]),
            _volume("火种归位", "第二卷摘要", "噬火者来袭", ["火起", "对峙", "归位"]),
        ],
    }
    provider = _FakeProvider([json.dumps(payload, ensure_ascii=False)])
    _install(monkeypatch, provider, [{"finish_reason": "stop", "audit_id": "a1"}])

    result = await ai_service.generate_outline(_request(6))

    assert result["mode"] == "llm"
    assert result["error_code"] is None
    assert len(provider.calls) == 1, "未截断时不许拆分调用"
    assert result["mainSummary"] == "全书主线摘要"
    assert _chapter_titles(result) == [
        "第1章 鬼火初现", "第2章 无名女尸", "第3章 丝线牵引",
        "第4章 火起", "第5章 对峙", "第6章 归位",
    ]


@pytest.mark.asyncio
async def test_truncated_outline_is_regenerated_per_volume(monkeypatch):
    """截断 → 按卷重来，产出完整大纲（卷名/弧/章节名全部为真，不回落兜底）。"""
    truncated = '{"mainSummary": "被截断", "volumes": [{"title": "第一'
    vol1 = {"mainSummary": "全书主线摘要", "volumes": [
        _volume("初火照影", "第一卷摘要", "丝线初现", ["鬼火初现", "无名女尸", "丝线牵引"]),
    ]}
    vol2 = {"volumes": [
        _volume("火种归位", "第二卷摘要", "噬火者来袭", ["火起", "对峙", "归位"]),
    ]}
    provider = _FakeProvider([
        truncated,
        json.dumps(vol1, ensure_ascii=False),
        json.dumps(vol2, ensure_ascii=False),
    ])
    _install(monkeypatch, provider, [
        {"finish_reason": "length", "audit_id": "trunc"},
        {"finish_reason": "stop", "audit_id": "vol-1"},
        {"finish_reason": "stop", "audit_id": "vol-2"},
    ])

    result = await ai_service.generate_outline(_request(6))

    # 1 次首轮 + 2 次分卷
    assert len(provider.calls) == 3
    assert "第 1 卷" in provider.calls[1]["user"]
    assert "第 2 卷" in provider.calls[2]["user"]
    # 分卷调用用的是分卷专用系统提示，且明确只要一卷
    assert "恰好包含 1 项" in provider.calls[1]["system"]

    assert result["mode"] == "llm", "分卷成功不得被当成降级"
    assert result["error_code"] is None
    assert result["mainSummary"] == "全书主线摘要"

    volumes = [n["title"] for n in result["nodes"] if n.get("type") == "volume"]
    arcs = [n["title"] for n in result["nodes"] if n.get("type") == "arc"]
    assert volumes == ["初火照影", "火种归位"]
    assert arcs == ["丝线初现", "噬火者来袭"], "弧必须是真弧，不能回落成「本卷主线弧」"
    assert "本卷主线弧" not in arcs
    assert _chapter_titles(result) == [
        "第1章 鬼火初现", "第2章 无名女尸", "第3章 丝线牵引",
        "第4章 火起", "第5章 对峙", "第6章 归位",
    ]
