"""角色档案分析（AI 通读正文 + 大纲，逐字段给建议）的测试。

产品不变量：
- AI 只产出**建议**，绝不自动覆盖作者手写内容；是否采用由作者在前端逐项决定。
- AI 不可用或返回不合法时给出中文原因，**绝不用规则引擎编造角色资料**。
- 只对「正文/大纲里确实有依据」的字段给建议；没证据的字段省略掉。
- 年龄/性别只能推断，返回值会在 `inferred` 里标出来。

本文件同时承担两件事：
1. 主路径与字段归一化的正确性；
2. 对抗性输入（污染文本、越界值、非字符串、超大输入、权限边界）下的行为。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.llm.provider import LLMConfigurationError, LLMResponseError
from app.llm.roles import ROLE_PLANNING
from app.services import character_profile_service as service
from app.utils.profile_text import clean_profile_text
from main import _request_windows, app


OWNER = {"X-NovelCraft-Invite": "owner-token"}
OTHER = {"X-NovelCraft-Invite": "other-token"}

# 清洗器会剥掉首尾的孤立标点（与「合法文本只允许去掉句末句号」的既有约定一致），
# 所以断言用不带句末句号的常量。
AI_PERSONALITY = "她习惯先观察再开口，压力越大越安静，但对熟人会表现出意外的固执"


# ── 测试替身 ────────────────────────────────────────────────

class _RecordingProvider:
    """替代 provider：记录每次调用的 messages，并按需返回结果或抛错。"""

    def __init__(self, reply: str | None = None, error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    async def chat(self, messages, role=None, **kwargs):
        self.calls.append({"messages": messages, "role": role})
        if self.error is not None:
            raise self.error
        return self.reply or ""


def _chapters(count: int = 2, content: str = "林晚把伞递过去，什么也没说。") -> list[dict]:
    return [
        {"title": f"标题{index}", "content": content, "order": index}
        for index in range(1, count + 1)
    ]


def _outline(main_summary: str = "林晚追查旧案，最终却发现线索指向自己。") -> dict:
    return {
        "structure": "three_act",
        "premise": "一个不肯回头的调查者。",
        "mainSummary": main_summary,
        "nodes": [
            {"id": "v1", "type": "volume", "title": "第一卷", "summary": "入局", "order": 1},
            {"id": "c1", "type": "chapter", "title": "第一章 雨夜", "summary": "林晚接到匿名信", "order": 1},
            {"id": "c2", "type": "chapter", "title": "第二章 旧档", "summary": "档案被人动过", "order": 2},
        ],
    }


def _project(*, characters: list[dict] | None = None, outline: dict | None = None) -> dict:
    return {
        "id": "p-1",
        "name": "测试项目",
        "characters": characters if characters is not None else [{"name": "林晚"}],
        "outline": outline,
    }


def _patch(monkeypatch, *, project: dict, provider: _RecordingProvider, chapters: list[dict] | None = None):
    monkeypatch.setattr(service.projects_dao, "get_project", lambda project_id: project)
    monkeypatch.setattr(service, "load_chapters", lambda project_id: chapters or [])
    monkeypatch.setattr(service, "get_runtime_provider", lambda _llm: provider)


def _reply(**fields) -> str:
    return json.dumps({"fields": fields}, ensure_ascii=False)


# ── 清洗工具 ───────────────────────────────────────────────

def test_clean_strips_mbti_and_score_residue():
    cleaned = clean_profile_text("INTJ，外向度：3，亲和度 7 分，7/10。她话少但记性极好。")
    for banned in ("INTJ", "外向度", "亲和度", "7/10"):
        assert banned not in cleaned, f"{banned} 应被清洗掉，实际：{cleaned}"
    assert "她话少但记性极好" in cleaned


def test_clean_strips_mbti_case_variants_and_doubled_tail():
    for variant in ("infj", "Intj", "INtJ", "ESTJJ"):
        cleaned = clean_profile_text(f"她是 {variant} 类型，习惯先观察再开口。")
        assert variant.lower() not in cleaned.lower(), f"MBTI 残留：{cleaned!r}"


def test_clean_does_not_mangle_time_units():
    """回归：「20 分钟」里的「分」是时间单位，不是分数。"""
    text = "她做事很有耐心，能坐着等 20 分钟不说话，也不催人。"
    assert clean_profile_text(text) == text.rstrip("。")


@pytest.mark.parametrize(
    "text",
    [
        "她比较怕面对温暖，遇事习惯先退半步，再做判断。",
        "她每天写 3000 字，从不间断，也很少抱怨。",
        "她住在 13 号楼 502 室，一个人住了很多年。",
        "他话不多，但每次开口都直指要害。",
    ],
)
def test_clean_does_not_touch_legitimate_prose(text):
    """合法描述只允许去掉句末句号，其余一字不动。"""
    assert clean_profile_text(text) == text.rstrip("。")


@pytest.mark.parametrize("noise", ["！" * 12, "？" * 12, "_" * 14, "   ", ""])
def test_clean_rejects_symbol_only_noise(noise):
    assert clean_profile_text(noise) == ""


@pytest.mark.parametrize("value", [None, 42, {"a": 1}, ["x"], True])
def test_clean_rejects_non_string_values(value):
    """非字符串不得被 str() 成 repr 当成合法内容。"""
    assert clean_profile_text(value) == ""


def test_clean_respects_max_length():
    assert len(clean_profile_text("她很安静，" * 60, max_length=120)) <= 120


# ── 主路径 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_returns_multi_field_suggestions(monkeypatch):
    provider = _RecordingProvider(reply=_reply(
        personality=AI_PERSONALITY,
        age="31",
        gender="女",
        appearance="黑色长发，左脸有一道旧疤",
        identity="档案调查员",
        arc="从只想查清旧案，转成不得不面对自己的过去",
    ))
    _patch(monkeypatch, project=_project(outline=_outline()), provider=provider, chapters=_chapters(3))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["degraded"] is False
    assert result["reason"] is None
    assert result["suggestions"]["personality"] == AI_PERSONALITY
    # 年龄归一化成整数，性别归一化成枚举
    assert result["suggestions"]["age"] == 31
    assert result["suggestions"]["gender"] == "女"
    assert result["suggestions"]["identity"] == "档案调查员"
    assert result["rejected"] == {}
    # 没给建议的字段要出现在 omitted，便于前端说明「证据不足」
    assert "catchphrase" in result["omitted"]
    assert result["basis"]["chapterCount"] == 3
    assert result["basis"]["hasOutline"] is True


@pytest.mark.asyncio
async def test_inferred_fields_are_flagged(monkeypatch):
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY, age=28, gender="男"))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["inferred"] == ["age", "gender"]


@pytest.mark.asyncio
async def test_out_of_range_age_is_rejected_not_clamped(monkeypatch):
    """年龄越界宁可拒掉让作者自己填，也不要悄悄改成别的数字。"""
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY, age=9999))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert "age" not in result["suggestions"]
    assert "超出合理范围" in result["rejected"]["age"]
    assert result["degraded"] is False


@pytest.mark.asyncio
async def test_non_numeric_age_is_rejected(monkeypatch):
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY, age="大概三十出头"))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert "age" not in result["suggestions"]
    assert result["rejected"]["age"]


@pytest.mark.asyncio
async def test_gender_is_normalized_from_natural_language(monkeypatch):
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY, gender="女性"))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["suggestions"]["gender"] == "女"


@pytest.mark.asyncio
async def test_gender_junk_is_rejected(monkeypatch):
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY, gender="未出道"))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert "gender" not in result["suggestions"]
    assert result["rejected"]["gender"]


@pytest.mark.asyncio
async def test_non_string_field_is_rejected(monkeypatch):
    provider = _RecordingProvider(reply=json.dumps(
        {"fields": {"personality": AI_PERSONALITY, "background": {"text": "她很安静。"}}},
        ensure_ascii=False,
    ))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert "background" not in result["suggestions"]
    assert result["rejected"]["background"]


@pytest.mark.asyncio
async def test_polluted_field_is_cleaned_not_dropped(monkeypatch):
    provider = _RecordingProvider(reply=_reply(
        personality="INTJ，外向度：8，她话少但记性极好，被逼急时会突然强硬。",
        values="开放度：7，秩序与责任。",
    ))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert "INTJ" not in result["suggestions"]["personality"]
    assert "外向度" not in result["suggestions"]["personality"]
    assert "开放度" not in result["suggestions"]["values"]
    assert "秩序与责任" in result["suggestions"]["values"]


@pytest.mark.asyncio
async def test_long_field_is_capped(monkeypatch):
    provider = _RecordingProvider(reply=_reply(
        personality=AI_PERSONALITY, catchphrase="她其实不想说这句话" * 30
    ))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["degraded"] is False
    assert len(result["suggestions"]["catchphrase"]) <= service.PROFILE_FIELDS["catchphrase"]["max"]


# ── 提示词契约 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prompt_carries_both_outline_and_prose(monkeypatch):
    """需求核心：AI 必须同时看到「文章」和「大纲」。"""
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY))
    _patch(monkeypatch, project=_project(outline=_outline()), provider=provider, chapters=_chapters(2))

    await service.analyze_character_profile("p-1", "林晚")

    call = provider.calls[0]
    assert call["role"] == ROLE_PLANNING
    prompt = json.dumps(call["messages"], ensure_ascii=False)
    assert "【作品大纲】" in prompt and "【正文节选】" in prompt
    assert "林晚追查旧案" in prompt       # 大纲侧内容
    assert "第一章 雨夜" in prompt
    assert "林晚把伞递过去" in prompt     # 正文侧内容
    assert "林晚" in prompt
    # 提示词必须明确禁用 MBTI 与打分
    system = call["messages"][0]["content"]
    assert "MBTI" in system
    assert "严禁输出任何数字评分" in system


@pytest.mark.asyncio
async def test_prompt_lists_every_field_key(monkeypatch):
    """字段清单由后端定义，提示词必须把键名原样告诉模型。"""
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    await service.analyze_character_profile("p-1", "林晚")

    system = provider.calls[0]["messages"][0]["content"]
    for key, spec in service.PROFILE_FIELDS.items():
        assert key in system, f"提示词缺少字段 {key}"
        assert spec["label"] in system


def test_outline_text_is_capped():
    nodes = [
        {"id": f"c{i}", "type": "chapter", "title": f"第{i}章", "summary": "梗概" * 40, "order": i}
        for i in range(1, 60)
    ]
    text = service._build_outline_text({"premise": "前提", "mainSummary": "总纲", "nodes": nodes})

    assert len(text) <= service.OUTLINE_MAX_CHARS
    assert "第21章" not in text


def test_outline_text_falls_back_when_no_chapter_nodes():
    text = service._build_outline_text({
        "premise": "前提",
        "mainSummary": "总纲",
        "nodes": [{"id": "v1", "type": "volume", "title": "第一卷", "summary": "入局", "order": 1}],
    })
    assert "第一卷" in text


def test_outline_text_empty_for_missing_outline():
    assert service._build_outline_text(None) == ""
    assert service._build_outline_text("不是字典") == ""
    assert service._build_outline_text({}) == ""


@pytest.mark.asyncio
async def test_prompt_stays_bounded_with_twelve_very_long_chapters(monkeypatch):
    """章节数 ≤ 12 且总量远超 4 万字时，正文侧也必须有硬性预算。"""
    chapters = [
        {"title": f"第{i}章", "content": "林晚把伞递过去，什么也没说。" * 1000, "order": i}
        for i in range(1, 13)
    ]
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=chapters)

    await service.analyze_character_profile("p-1", "林晚")

    prompt = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
    assert len(prompt) <= 60_000, f"提示词 {len(prompt)} 字符，没有预算上限"


def test_bound_prose_keeps_both_ends():
    bounded = service._bound_prose("开" + "中" * 5000 + "尾", limit=100)
    assert bounded.startswith("开")
    assert bounded.endswith("尾")
    assert len(bounded) <= 100 + 20


# ── 降级：绝不编造 ─────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reply"),
    [
        ("非 JSON 的散文", "我觉得她是一个很复杂的角色，说来话长。"),
        ("空 fields", '{"fields":{}}'),
        ("缺 fields 字段", '{"description":"她很安静，习惯先观察。"}'),
        ("只有 MBTI", '{"fields":{"personality":"INTJ"}}'),
        ("只有打分", '{"fields":{"personality":"外向度：8，亲和度：7 分，7/10，68%"}}'),
        ("只有标点", '{"fields":{"personality":"。。。。。。"}}'),
        ("fields 是数组", '{"fields":[]}'),
        ("全部字段被拒", '{"fields":{"age":"不知道","gender":"未出道"}}'),
    ],
)
async def test_bad_reply_degrades_with_chinese_reason(monkeypatch, label, reply):
    provider = _RecordingProvider(reply=reply)
    _patch(monkeypatch, project=_project(outline=_outline()), provider=provider, chapters=_chapters(2))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["degraded"] is True, f"{label} 应降级"
    assert result["suggestions"] == {}, f"{label} 不得产出任何建议"
    assert result["reason"] and any("\u4e00" <= ch <= "\u9fff" for ch in result["reason"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [LLMResponseError("返回内容过短"), LLMConfigurationError("未配置")],
    ids=["response_error", "configuration_error"],
)
async def test_provider_failure_degrades_without_raising(monkeypatch, error):
    provider = _RecordingProvider(error=error)
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(2))

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["degraded"] is True
    assert result["suggestions"] == {}
    assert result["reason"]


@pytest.mark.asyncio
async def test_no_chapters_skips_llm_entirely(monkeypatch):
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY))
    _patch(monkeypatch, project=_project(outline=_outline()), provider=provider, chapters=[])

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["degraded"] is True
    assert "还没有" in result["reason"]
    assert result["basis"]["chapterCount"] == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_blank_chapter_content_skips_llm_entirely(monkeypatch):
    """有章节行但正文全空：不能因为抽样里含「第N章 标题」就去调模型。"""
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY))
    _patch(
        monkeypatch,
        project=_project(),
        provider=provider,
        chapters=[{"title": "空的", "content": "   \n ", "order": 1}],
    )

    result = await service.analyze_character_profile("p-1", "林晚")

    assert result["degraded"] is True
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unknown_character_raises(monkeypatch):
    provider = _RecordingProvider(reply=_reply(personality=AI_PERSONALITY))
    _patch(monkeypatch, project=_project(), provider=provider, chapters=_chapters(1))

    with pytest.raises(service.CharacterNotFoundError):
        await service.analyze_character_profile("p-1", "不存在的人")
    assert provider.calls == []


# ── API 层 ─────────────────────────────────────────────────

def _configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "owner-token:user-a,other-token:user-b")
    _request_windows.clear()


def _create_project_with_character(client: TestClient, description: str = "") -> str:
    created = client.post(
        "/api/projects/",
        headers=OWNER,
        json={"name": "角色档案测试", "genre": "mystery", "targetWordCount": 10000, "stylePreset": "epic"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["data"]["id"]
    saved = client.patch(
        f"/api/projects/{project_id}",
        headers=OWNER,
        json={"characters": [{
            "id": "char-1", "name": "林晚", "role": "protagonist",
            "personality": {"description": description},
        }]},
    )
    assert saved.status_code == 200, saved.text
    return project_id


def test_api_unknown_character_returns_404(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project_with_character(client)
        response = client.post(
            f"/api/projects/{project_id}/analyze-character-profile",
            headers=OWNER,
            json={"characterName": "查无此人"},
        )

    assert response.status_code == 404
    assert "查无此人" in response.json()["detail"]


def test_api_non_owner_returns_403(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project_with_character(client)
        response = client.post(
            f"/api/projects/{project_id}/analyze-character-profile",
            headers=OTHER,
            json={"characterName": "林晚"},
        )

    assert response.status_code == 403


@pytest.mark.parametrize("payload", [{"characterName": ""}, {}, {"characterName": "x" * 200}])
def test_api_rejects_invalid_character_name(monkeypatch, payload):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project_with_character(client)
        response = client.post(
            f"/api/projects/{project_id}/analyze-character-profile",
            headers=OWNER,
            json=payload,
        )

    assert response.status_code == 422


def test_api_degrades_without_chapters_and_never_calls_provider(monkeypatch):
    _configure(monkeypatch)
    spy = MagicMock(side_effect=AssertionError("不应在无正文时获取 provider"))
    with TestClient(app) as client:
        project_id = _create_project_with_character(client)
        monkeypatch.setattr(service, "get_runtime_provider", spy)
        response = client.post(
            f"/api/projects/{project_id}/analyze-character-profile",
            headers=OWNER,
            json={"characterName": "林晚"},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["degraded"] is True
    assert data["suggestions"] == {}
    assert data["reason"]
    spy.assert_not_called()


def test_api_analysis_is_advisory_only_and_never_writes(monkeypatch):
    """分析请求不得改动项目数据：作者手写内容必须一字不差。"""
    _configure(monkeypatch)
    hand_written = "她表面上随和，其实非常固执。"
    with TestClient(app) as client:
        project_id = _create_project_with_character(client, description=hand_written)
        before = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["characters"]

        monkeypatch.setattr(service, "load_chapters", lambda _pid: [])
        response = client.post(
            f"/api/projects/{project_id}/analyze-character-profile",
            headers=OWNER,
            json={"characterName": "林晚"},
        )
        after = client.get(f"/api/projects/{project_id}", headers=OWNER).json()["data"]["characters"]

    assert response.status_code == 200
    assert after == before
    assert after[0]["personality"]["description"] == hand_written


def test_api_padded_character_name_is_matched_after_trim(monkeypatch):
    _configure(monkeypatch)
    with TestClient(app) as client:
        project_id = _create_project_with_character(client)
        monkeypatch.setattr(service, "load_chapters", lambda _pid: [])
        response = client.post(
            f"/api/projects/{project_id}/analyze-character-profile",
            headers=OWNER,
            json={"characterName": "  林晚  "},
        )

    # 去掉首尾空格后能匹配到角色，因此走的是「无正文降级」而不是 404
    assert response.status_code == 200, response.text
    assert response.json()["data"]["degraded"] is True
