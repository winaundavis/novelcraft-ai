"""建议1 去 AI 味后处理（deai_service）单元测试。

覆盖：
- deai_rewrite 组装 prompt：含 anti_ai_rules 清单 + 检测结果（句式/开头/连接词/场景）
- LLM mock 成功：返回重写文本 + changes + score 前后统计 + ROLE_WRITING 角色路由
- LLM 失败（LLMConfigurationError）：降级返回原文（degraded=True），不抛异常
- LLM 返回过短：同样降级原文
- 无画像（get_profile=None）：prompt 只用检测结果，无反 AI 规则段
- 空文本：直接返回，不调用 LLM
- 长文本：只重写前 DEAI_MAX_INPUT_CHARS 窗口，尾部原样保留拼接
- API：POST /api/ai/deai 成功 / 项目不存在 404 / 缺字段 422
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app

from app.llm.provider import LLMConfigurationError
from app.llm.roles import ROLE_WRITING
from app.services.deai_service import (
    DEAI_MAX_INPUT_CHARS,
    DEAI_MAX_TOKENS,
    _build_deai_prompt,
    _window_max_tokens,
    deai_rewrite,
)


def _profile_with_rules() -> dict:
    """带反 AI 规则目录的画像（camelCase 键与 style_dao.row_to_profile 对齐）。"""
    return {
        "id": "profile-1",
        "antiAiRules": [
            {"rule": "避免连续章节以相同句式或相同场景开头。", "key": "avoid_repetitive_openings"},
            {"rule": "避免过度使用「仿佛/然而/顿时/突然/不由得」等连接词。", "key": "avoid_connective_overuse"},
        ],
    }


def _detection_payload() -> dict:
    """M3 检测结果样例（句式重复 high + 连接词滥用 medium）。"""
    return {
        "issues": [
            {
                "severity": "high", "type": "句式重复",
                "reason": "句子开头重复率 60%", "suggestion": "打散相同句子开头",
            },
            {
                "severity": "medium", "type": "连接词滥用",
                "reason": "每 500 字 7 个 AI 连接词", "suggestion": "删减冗余连接词",
            },
        ],
        "debt_items": [
            {"type": "sentence_repeat", "severity": "high"},
            {"type": "connective_overuse", "severity": "medium"},
        ],
        "needs_rewrite": True,
        "score": 70,
        "opening": "",
        "previous_opening": "",
    }


# ══════════════════════════════════════════════════════
# prompt 组装
# ══════════════════════════════════════════════════════

def test_prompt_includes_anti_ai_rules_and_detection():
    prompt = _build_deai_prompt("正文内容", ["规则一", "规则二"], _detection_payload())
    assert "反 AI 规则清单" in prompt
    assert "规则一" in prompt
    assert "检测到的 AI 味问题" in prompt
    assert "句式重复" in prompt
    assert "连接词滥用" in prompt
    assert "正文内容" in prompt


def test_prompt_without_rules_uses_detection_only():
    """无画像（anti_ai_rules 为空）时只用检测结果，不带反 AI 规则段。"""
    prompt = _build_deai_prompt("正文内容", [], _detection_payload())
    assert "反 AI 规则清单" not in prompt
    assert "检测到的 AI 味问题" in prompt
    assert "句式重复" in prompt  # 检测结果仍注入


# ══════════════════════════════════════════════════════
# deai_rewrite 主流程
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deai_rewrite_success_with_rules_and_role():
    """画像规则 + 检测结果注入 prompt，走 ROLE_WRITING 角色路由，返回重写文本与统计。"""
    rewritten = "去味后的完整正文。句式更加多样，节奏更加自然，读起来流畅多了。"
    with patch("app.services.deai_service.get_profile", return_value=_profile_with_rules()), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value=rewritten)) as mock_chat:
        result = await deai_rewrite("proj-1", "原文正文内容。")

    assert result["result"] == rewritten
    assert result["degraded"] is False
    assert result["scoreBefore"] == 70
    assert result["ruleCount"] == 2
    assert result["issueCount"] == 2
    assert {item["type"] for item in result["changes"]} == {"句式重复", "连接词滥用"}

    # ROLE_WRITING 角色路由
    _, kwargs = mock_chat.call_args
    assert kwargs["role"] == ROLE_WRITING
    # user prompt 同时含反 AI 规则与检测结果
    user_content = mock_chat.call_args.args[0][1]["content"]
    assert "避免连续章节以相同句式" in user_content
    assert "句式重复" in user_content
    assert "连接词滥用" in user_content


@pytest.mark.asyncio
async def test_deai_rewrite_no_profile_detection_only_via_llm():
    """无画像时 deai_rewrite 只注入检测结果，ruleCount=0。"""
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value="重写结果内容足够长不会触发降级。")) as mock_chat:
        result = await deai_rewrite("proj-1", "原文")

    assert result["ruleCount"] == 0
    user_content = mock_chat.call_args.args[0][1]["content"]
    assert "反 AI 规则清单" not in user_content
    assert "检测到的 AI 味问题" in user_content


@pytest.mark.asyncio
async def test_deai_rewrite_degraded_on_llm_error():
    """LLM 失败（未配置/调用异常）→ 返回原文 + degraded=True，不抛异常。"""
    with patch("app.services.deai_service.get_profile", return_value=_profile_with_rules()), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch(
             "app.services.deai_service.llm.chat",
             new=AsyncMock(side_effect=LLMConfigurationError("no key")),
         ):
        result = await deai_rewrite("proj-1", "原文正文内容。")

    assert result["result"] == "原文正文内容。"
    assert result["degraded"] is True
    assert result["ruleCount"] == 2  # 规则仍被统计，但降级保留原文


@pytest.mark.asyncio
async def test_deai_rewrite_degraded_on_short_result():
    """LLM 返回过短（视为无效）→ 降级原文。"""
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value="短")):
        result = await deai_rewrite("proj-1", "原文正文内容。")

    assert result["result"] == "原文正文内容。"
    assert result["degraded"] is True


@pytest.mark.asyncio
async def test_deai_rewrite_empty_text_skips_llm():
    """空文本直接返回，不调用 LLM。"""
    with patch("app.services.deai_service.llm.chat", new=AsyncMock()) as mock_chat:
        result = await deai_rewrite("proj-1", "   ")

    assert result["result"] == "   "
    assert result["degraded"] is False
    mock_chat.assert_not_called()


@pytest.mark.asyncio
async def test_deai_rewrite_long_text_segments_all_windows():
    """超长正文：整章按窗口分段，每段都去味（不只前窗口），全量处理。"""
    original = "窗" * DEAI_MAX_INPUT_CHARS + "\n" + "尾" * 500

    def _proportional_rewrite(messages, **_kwargs):
        # 每个窗口的重写结果与该窗口等长，才能通过幅面保真校验
        body = messages[1]["content"].split("【正文】")[-1].rsplit("要求：", 1)[0].strip()
        return "重" * len(body)

    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(side_effect=_proportional_rewrite)) as mock_chat:
        result = await deai_rewrite("proj-1", original)

    # 全量去味：两个窗口都被重写（LLM 调用 2 次），尾部不再原样保留
    assert mock_chat.call_count == 2
    assert result["result"] == "重" * DEAI_MAX_INPUT_CHARS + "\n\n" + "重" * 500
    assert "尾" not in result["result"]
    assert result["segments"] == 2
    assert result["keptWindows"] == 0


@pytest.mark.asyncio
async def test_deai_rewrite_keeps_original_when_model_collapses_text():
    """2026-10-20 实测事故回归：6029 字正文被模型压缩成 150 字，旧判定会当成成功。

    去味只能优化表达，不能把整章缩写成摘要——否则前端会把被删掉 97% 的正文写回项目。
    """
    original = "原" * 6000
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value="缩" * 150)):
        result = await deai_rewrite("proj-1", original)

    assert result["result"] == original
    assert result["degraded"] is True
    assert result["keptWindows"] == 1


@pytest.mark.asyncio
async def test_deai_rewrite_keeps_original_when_output_truncated():
    """finish_reason=length（输出被截断）→ 保留原文，不能把半截正文写回。"""
    original = "原" * 3000
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.get_llm_call_meta", return_value={"finish_reason": "length"}), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value="改" * 2800)):
        result = await deai_rewrite("proj-1", original)

    assert result["result"] == original
    assert result["degraded"] is True
    assert result["keptWindows"] == 1


@pytest.mark.asyncio
async def test_deai_passes_previous_chapter_text_for_cross_chapter_rules():
    """传 chapterId 时必须把上一章正文交给检测，否则「开头重复 / 场景词组」永不命中。"""
    original = "原文正文内容。"
    detection = _detection_payload()
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service._load_previous_chapter_text", return_value="上一章结尾正文"), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=detection) as mock_detect, \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value="去味后的正文，长度相当。")):
        await deai_rewrite("proj-1", original, "ch-1")

    # 首次调用才是“检测正文”那一次（最后一次是对结果重新评分）
    assert mock_detect.call_args_list[0].args[:2] == (original, "上一章结尾正文")


@pytest.mark.asyncio
async def test_deai_rewrite_keeps_original_when_model_expands_text():
    """2026-10-20 实测：255 字被扩写成 501 字（1.96 倍）——“去味”不应新增情节。"""
    original = "原" * 1000
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.get_llm_call_meta", return_value={"finish_reason": "stop"}), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value="改" * 2000)):
        result = await deai_rewrite("proj-1", original)

    assert result["result"] == original
    assert result["degraded"] is True
    assert result["keptWindows"] == 1


@pytest.mark.asyncio
async def test_deai_rewrite_keeps_original_when_dialogue_count_drifts():
    """把四句对白改成叙述 = 改事实，必须保留原文。"""
    dialogue = "“你来了。”她说。“嗯。”他答。“路上顺利？”“还行。”"
    original = dialogue + "雨声压着屋檐。" + "门外没有动静，" * 40
    rewritten = "她问他来不来，他答了一声，又问了路上是否顺利，他说还行。" + "门外没有动静，" * 40
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.get_llm_call_meta", return_value={"finish_reason": "stop"}), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value=rewritten)):
        result = await deai_rewrite("proj-1", original)

    assert result["result"] == original
    assert result["degraded"] is True


@pytest.mark.asyncio
async def test_deai_rewrite_accepts_faithful_rewrite():
    """只删掉高频模糊词、篇幅与台词都不变 → 应被接受。"""
    original = "他缓缓推开门，仿佛空气都凝固了。" + "门轴没有声音，" * 40
    rewritten = original.replace("缓缓", "").replace("仿佛", "")
    with patch("app.services.deai_service.get_profile", return_value=None), \
         patch("app.services.deai_service.detect_ai_artifacts", return_value=_detection_payload()), \
         patch("app.services.deai_service.get_llm_call_meta", return_value={"finish_reason": "stop"}), \
         patch("app.services.deai_service.llm.chat", new=AsyncMock(return_value=rewritten)):
        result = await deai_rewrite("proj-1", original)

    assert result["result"] == rewritten
    assert result["degraded"] is False
    assert result["keptWindows"] == 0


def test_window_max_tokens_scales_with_window_and_caps():
    assert _window_max_tokens("字" * 100) == 1124
    assert _window_max_tokens("字" * DEAI_MAX_INPUT_CHARS) == DEAI_MAX_INPUT_CHARS + 1024
    assert _window_max_tokens("字" * (DEAI_MAX_INPUT_CHARS * 3)) == DEAI_MAX_TOKENS


# ══════════════════════════════════════════════════════
# API 端点
# ══════════════════════════════════════════════════════

def _unique_name(prefix: str = "deai") -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def _create_project(client: TestClient) -> dict:
    resp = client.post(
        "/api/projects/",
        json={"name": _unique_name(), "genre": "xuanhuan"},
    )
    assert resp.status_code == 200, f"创建项目失败: {resp.text}"
    return resp.json()["data"]


def _delete_project(client: TestClient, project_id: str) -> None:
    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code in (200, 404, 409), f"删除失败: {resp.status_code}"


def test_deai_api_success():
    """POST /api/ai/deai：正常路径返回 {result, changes, degraded, ...}。"""
    payload = {
        "result": "去味后的正文内容。",
        "changes": [{"type": "句式重复", "severity": "high", "note": "句子开头重复率 60%"}],
        "degraded": False,
        "scoreBefore": 70,
        "scoreAfter": 92,
        "ruleCount": 2,
        "issueCount": 1,
    }
    with patch("app.api.ai.deai_rewrite", new=AsyncMock(return_value=payload)):
        with TestClient(app) as client:
            project = _create_project(client)
            try:
                resp = client.post("/api/ai/deai", json={"projectId": project["id"], "text": "原文正文内容。"})
                assert resp.status_code == 200
                body = resp.json()
                assert body["success"] is True
                data = body["data"]
                assert data["result"] == "去味后的正文内容。"
                assert data["degraded"] is False
                assert data["scoreBefore"] == 70
                assert data["scoreAfter"] == 92
                assert data["changes"][0]["type"] == "句式重复"
            finally:
                _delete_project(client, project["id"])


def test_deai_api_project_not_found():
    """项目不存在 → 404，不调用 LLM。"""
    with patch("app.api.ai.deai_rewrite", new=AsyncMock()) as mock_deai:
        with TestClient(app) as client:
            resp = client.post("/api/ai/deai", json={"projectId": "nonexistent-id", "text": "正文"})
        assert resp.status_code == 404
        mock_deai.assert_not_called()


def test_deai_api_missing_fields():
    """缺 projectId → 422。"""
    with TestClient(app) as client:
        resp = client.post("/api/ai/deai", json={"text": "正文"})
    assert resp.status_code == 422
