"""M3 AI 味检测（确定性规则，零 LLM 成本）—— review_service.detect_ai_artifacts 单测。

覆盖：开头重复 / 句式重复 / 连接词滥用 / 场景关键词组 四类确定性规则，
以及超阈值后的开头局部重写 rewrite_opening（LLM 打桩）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.llm.provider import LLMConfigurationError
from app.services import review_service
from app.services.review_service import (
    AI_OPENING_REWRITE_LENGTH,
    detect_ai_artifacts,
    rewrite_opening,
)


def _long_content() -> str:
    """构造超过开头重写窗口的正文（前文 + 长结尾）。"""
    return ("开头段落。" * 200) + ("结尾段落。" * 200)


def _ai_payload() -> dict:
    return {
        "issues": [
            {
                "severity": "high", "type": "开头重复",
                "reason": "开头 30 字与上章相似度 80%", "suggestion": "改写开头",
            }
        ],
        "debt_items": [{"type": "opening_repeat", "severity": "high", "detail": "相似度 80%"}],
        "needs_rewrite": True,
        "score": 80,
    }


def test_empty_text_scores_100():
    result = detect_ai_artifacts("")
    assert result["score"] == 100
    assert result["debt_items"] == []
    assert result["needs_rewrite"] is False


def test_normal_text_no_artifacts():
    text = (
        "陈墨沿着青石路走到镇口，天色已经暗下来。"
        "他抬头看了一眼远处的山影，把斗笠压得更低。"
        "镇口的茶摊还亮着灯，掌柜正在收拾碗筷。"
        "陈墨没有停留，径直往东边的小巷走去。"
        "巷子尽头是一扇黑漆木门，门环上缠着红绳。"
    )
    result = detect_ai_artifacts(text, previous_text="昨夜的风吹过竹林，沙沙作响。")
    assert result["debt_items"] == []
    assert result["needs_rewrite"] is False


def test_opening_repeat_detected():
    """开头 30 字与上一章高度重合 → opening_repeat 命中。"""
    opening = "陈墨推开那扇尘封的大门缓缓走入幽暗的走廊"
    text = opening + "。他停下脚步，侧耳倾听周围的动静。" * 3
    previous = opening + "。他环顾四周，光线从窗缝里漏进来。" * 3
    result = detect_ai_artifacts(text, previous_text=previous)
    types = {item["type"] for item in result["debt_items"]}
    assert "opening_repeat" in types
    assert result["needs_rewrite"] is True  # high 项触发局部重写


def test_sentence_repetition_detected():
    """≥50% 句子以相同前 4 字开头 → sentence_repeat（high）。"""
    # 4 种重复开头 × 2 = 8 句，repeated=4/8=0.5 ≥ 0.5
    text = (
        "他缓缓抬头。他缓缓抬头。陈墨握紧拳头。陈墨握紧拳头。"
        "窗外雨声渐密。窗外雨声渐密。房间灯光昏暗。房间灯光昏暗。"
    )
    result = detect_ai_artifacts(text)
    types = {item["type"] for item in result["debt_items"]}
    assert "sentence_repeat" in types
    assert any(item["severity"] == "high" for item in result["issues"])


# ══════════════════════════════════════════════════════
# P1-1：同章 n-gram 原句重复检测（验收 P1：AI 味检测盲区修复）
# ══════════════════════════════════════════════════════

def test_same_chapter_ngram_repeat_detected():
    """同章内原样重复的长句（≥10 字）→ same_chapter_ngram_repeat 命中。"""
    repeated_sentence = "晨光透过窗棂洒落在青石板上"
    text = (
        f"陈墨推开窗，{repeated_sentence}，泛着淡淡的金色。\n"
        f"他深吸一口气，{repeated_sentence}，仿佛昨日的疲惫都被洗净。\n"
        "他转身收拾行李，准备出发。"
    )
    result = detect_ai_artifacts(text)
    types = {item["type"] for item in result["debt_items"]}
    assert "same_chapter_ngram_repeat" in types
    # 命中后 score 应被扣分（不再是 100）
    assert result["score"] < 100


def test_same_chapter_ngram_repeat_not_false_positive():
    """正常行文（无整句重复）不误报同章 n-gram 重复。"""
    text = (
        "陈墨沿着青石路走到镇口，天色已经暗下来。"
        "他抬头看了一眼远处的山影，把斗笠压得更低。"
        "镇口的茶摊还亮着灯，掌柜正在收拾碗筷。"
        "陈墨没有停留，径直往东边的小巷走去。"
        "巷子尽头是一扇黑漆木门，门环上缠着红绳。"
    )
    result = detect_ai_artifacts(text)
    types = {item["type"] for item in result["debt_items"]}
    assert "same_chapter_ngram_repeat" not in types


def test_ngram_repeat_issue_carries_phrase_and_count():
    """n-gram 重复 issue 应携带重复片段与次数（供重写提示）。"""
    phrase = "父亲把烟斗在桌上磕了磕"
    text = (
        f"饭桌上，{phrase}，火星溅了几点。\n"
        f"他沉默片刻，{phrase}，才继续开口说话。\n"
        "烛火在风里晃了晃。"
    )
    result = detect_ai_artifacts(text)
    ngram_issues = [i for i in result["issues"] if i["type"] == "同章原句重复"]
    assert ngram_issues, "应检测到同章原句重复"
    assert phrase in ngram_issues[0]["original"]
    assert ngram_issues[0]["original"].count(phrase[:10]) >= 1
    # count 字段应 ≥2
    debt = next(d for d in result["debt_items"] if d["type"] == "same_chapter_ngram_repeat")
    assert "重复 2 次" in debt["detail"] or "重复 3 次" in debt["detail"]


def test_logic_connectives_are_not_treated_as_ai_flavor():
    """「但是 / 然而」是人类写作的正常转折词，不能算 AI 味（否则误报）。

    2026-10-20 网文创作评审：把逻辑转折词与「仿佛/似乎」这类模糊词混算，
    会误伤正常段落、放过真正的问题。
    """
    text = "但是他还是走了。然而天已经亮了。但是没有人回头。然而路还在往前。" * 6
    result = detect_ai_artifacts(text)
    types = {item["type"] for item in result["debt_items"]}
    assert "connective_overuse" not in types


def test_connective_overuse_detected():
    """每 500 字 ≥6 个 AI 连接词 → connective（medium）。"""
    text = (
        "他突然停下脚步，但是周围很安静。仿佛有人注视着他，然而并没有人。"
        "他缓缓地呼吸，似乎一切都正常。瞬间，他警觉起来。顿时，他握紧了拳头。"
        "竟然没有人发现他。不由得，他后退了一步。轻轻一声响，他立刻转身。"
        "但是依然空无一人。仿佛一切都是幻觉。他缓缓放松下来，似乎终于平静了。"
        "然而他并不知道，真正的危险才刚刚开始。突然，背后的门无风自开。"
    ) * 3
    result = detect_ai_artifacts(text)
    types = {item["type"] for item in result["debt_items"]}
    assert "connective_overuse" in types


def test_scene_cluster_detected():
    """前后两章同时出现同一组 ≥3 个场景关键词 → scene_cluster（medium）。"""
    text = "深夜，他独自坐在窗前，沉默地看着月光洒落，心中却无法平静。"
    previous = "深夜的街道上，他独自走着，沉默不语，月光把他的影子拉得很长。"
    result = detect_ai_artifacts(text, previous_text=previous)
    types = {item["type"] for item in result["debt_items"]}
    assert "scene_cluster" in types


# ══════════════════════════════════════════════════════
# C3：同章 n-gram 自动重写闭环（Codex 盘点）
# ══════════════════════════════════════════════════════

def test_ngram_long_repeat_is_high_severity():
    """≥20 字的完整句/段复制 → high（触发自动重写），而非 medium。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"  # 21 字
    text = (
        f"陈墨推开窗，{phrase}，他深吸一口气。\n"
        f"他再次望向窗外，{phrase}，仿佛一夜未眠。\n"
        "他转身收拾行李。"
    )
    result = detect_ai_artifacts(text)
    ngram_issues = [i for i in result["issues"] if i["type"] == "同章原句重复"]
    assert ngram_issues, "应检测到同章原句重复"
    assert ngram_issues[0]["severity"] == "high"
    assert result["needs_rewrite"] is True


def test_ngram_short_repeat_stays_medium():
    """10-19 字重复 → medium（不触发自动重写）。"""
    phrase = "他轻轻叹了口气"  # 7 字，低于 min_len → 不报
    text = (
        f"{phrase}，然后继续往前走。\n"
        f"{phrase}，像是放下了什么。\n"
        "夜色渐深。"
    )
    result = detect_ai_artifacts(text)
    ngram_issues = [i for i in result["issues"] if i["type"] == "同章原句重复"]
    # 7 字 < 10 字 min_len，不应报
    assert not ngram_issues


def test_ngram_allows_codes_and_citations():
    """编号/专名（YJ-1963-0417、书名）重复不应误判为 AI 味（Codex C3）。"""
    text = (
        "档案编号 YJ-1963-0417 首次出现在卷宗里。\n"
        "他反复核对档案编号 YJ-1963-0417，确认无误。\n"
        "《尘封往事》是他母亲留下的书。\n"
        "他又翻了一遍《尘封往事》，仍无收获。"
    )
    result = detect_ai_artifacts(text)
    types = {item["type"] for item in result["debt_items"]}
    assert "same_chapter_ngram_repeat" not in types, "编号/书名不应判为 AI 味重复"


@pytest.mark.asyncio
async def test_rewrite_ngram_repeat_removes_duplicate():
    """rewrite_ngram_repeat 按位置改写后，重复片段应消失（闭环落库前提）。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    content = (
        f"陈墨推开窗，{phrase}，他深吸一口气。\n"
        f"他再次望向窗外，{phrase}，仿佛一夜未眠。\n"
        "他转身收拾行李，准备出发。"
    )
    ai = {
        "issues": [
            {
                "type": "同章原句重复", "severity": "high",
                "original": phrase, "suggestion": "改写其中一处",
            }
        ],
        "score": 55,
    }
    # 改写后：第一处保留语义但换了表达，第二处不变 → 片段不再出现 2 次
    async def fake_chat(messages, **kwargs):
        return (
            "陈墨推开窗，微光从窗棂间漏进来，落在青石板上，"
            "像镀了一层淡金。他深吸一口气。"
        )

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 重复片段应不再出现 2 次（改写了其中一处所在窗口）
    assert result.count(phrase) < content.count(phrase), "重复应被改写消除"


@pytest.mark.asyncio
async def test_rewrite_ngram_repeat_degrades_on_failure():
    """LLM 失败 → 返回原文（降级不阻塞）。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    content = f"他推开窗，{phrase}。\n他再看，{phrase}。\n他离开。"
    ai = {
        "issues": [
            {"type": "同章原句重复", "severity": "high", "original": phrase}
        ],
        "score": 55,
    }
    with patch.object(
        review_service.llm, "chat",
        new=AsyncMock(side_effect=LLMConfigurationError("未配置")),
    ):
        result = await review_service.rewrite_ngram_repeat(content, ai)
    assert result == content


# ══════════════════════════════════════════════════════
# D1：n-gram 窗口坐标修复（Codex 三轮 P1）
# ══════════════════════════════════════════════════════

def test_extract_ngram_window_returns_start_end():
    """_extract_ngram_window 返回 (start, end, window)，窗口覆盖重复片段。"""
    text = "前文" * 300 + "重复片段AAA" + "后文" * 300
    pos = text.find("重复片段AAA")
    start, end, window = review_service._extract_ngram_window(text, pos, len("重复片段AAA"))
    assert window == text[start:end]
    assert "重复片段AAA" in window
    # 窗口包含前文和后文上下文
    assert "前文" in window and "后文" in window


@pytest.mark.asyncio
async def test_rewrite_ngram_repeat_preserves_far_content():
    """重复片段位于 >400 字处：改写后正文长度合理、窗口外前后文完整（Codex D1 坐标修复）。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"  # 21 字
    prefix = "开头叙述。" * 100  # 600 字（>400）
    suffix = "结尾叙述。" * 100  # 500 字
    content = (
        f"{prefix}\n{phrase}，他深吸一口气。\n"
        f"他再次望向窗外，{phrase}，仿佛一夜未眠。\n{suffix}"
    )
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    async def fake_chat(messages, **kwargs):
        return "改写后的句子：微光漏进窗棂，落在青石板上，像镀了层淡金。他深吸一口气。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # D1 核心：窗口只作 LLM 参考，替换只针对句子 → 窗口外正文完整保留
    assert result.startswith(prefix), "前缀被截断"
    assert result.endswith(suffix), "后缀被截断"
    # 正文长度合理（只替换一句，长度差应很小）
    assert abs(len(result) - len(content)) < 100, f"正文长度异常: {len(result)} vs {len(content)}"
    # 重复片段应减少（改写了一处所在句）
    assert result.count(phrase) < content.count(phrase)


@pytest.mark.asyncio
async def test_rewrite_ngram_repeat_handles_repeat_near_end():
    """重复片段接近正文结尾：不越界、后缀保留（Codex D1）。"""
    phrase = "他缓缓放下手中的信，看着窗外沉默了许久"  # 19 字
    prefix = "前文铺垫。" * 300
    content = (
        f"{prefix}\n{phrase}，这是第一次出现。\n"
        f"他想起昨晚，{phrase}，这是第二次出现。"
    )  # 片段接近结尾
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    async def fake_chat(messages, **kwargs):
        return "他搁下信，目光落向窗外，沉默良久。然后他缓缓转身，把信放回抽屉里，脚步声在空荡的房间里回荡，像在等待什么回音。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 句子级替换：前缀/后缀完整保留，正文长度合理
    assert result.startswith(prefix), "前缀被截断"
    assert len(result) > 100, "正文长度异常"
    assert result.count(phrase) < content.count(phrase)


@pytest.mark.asyncio
async def test_rewrite_ngram_repeat_multiple_repeats_keeps_plausible_length():
    """多个重复片段：逐一处理后正文长度保持合理（不膨胀/不截断）。"""
    phrase1 = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    phrase2 = "他把钥匙放进抽屉，指尖在锁孔边缘停留片刻"
    # 注意：不能把 "结尾收束。" * 50 放在相邻字符串字面量拼接序列里，
    # 否则 * 50 会作用于整个拼接结果（Python 隐式拼接陷阱），正文会被重复 50 次。
    content = (
        f"{phrase1}，他抬头。\n{phrase1}，他又低头。\n"
        f"{phrase2}，然后关上门。\n{phrase2}，最终离开。\n"
    ) + "结尾收束。" * 50
    ai = {
        "issues": [
            {"type": "同章原句重复", "severity": "high", "original": phrase1},
            {"type": "同章原句重复", "severity": "high", "original": phrase2},
        ],
        "score": 45,
    }

    async def fake_chat(messages, **kwargs):
        return "改写后的描述，保持语义一致但换一种表达方式，让文字读起来更像人类作者手写的自然笔触。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 句子级替换：正文长度偏差极小（只替换句子，不吞并窗口）
    assert abs(len(result) - len(content)) < 100
    # 结尾收束保留
    assert result.endswith("结尾收束。" * 50)


# ══════════════════════════════════════════════════════
# P2-1：n-gram 完整句子替换（Codex 四轮 P2-1）
#   - 片段位于句子中间 → 解析整句替换，避免"片段替换 + 句尾残留"
#   - LLM 返回完整句子 → 无双标点 / 无重复尾句 / 语句边界合理
#   - 无标点超长段 → 退回片段替换（不吞并正文）
# ══════════════════════════════════════════════════════

def test_ngram_sentence_bounds_parses_middle_sentence():
    """重复片段位于完整句子中间时，边界应覆盖整句（含句末标点）。"""
    text = "他推开门，晨光透过窗棂洒落在青石板上泛起淡金色的薄雾，然后转身坐下。"
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    pos = text.find(phrase)
    left, right = review_service._ngram_sentence_bounds(text, pos, len(phrase))
    sentence = text[left:right]
    assert sentence == "他推开门，晨光透过窗棂洒落在青石板上泛起淡金色的薄雾，然后转身坐下。", sentence
    assert sentence.endswith("。")
    # 边界应包含片段
    assert phrase in sentence


def test_ngram_sentence_bounds_parses_multiple_sentences():
    """片段后紧跟多个句子时，只扩到第一个句末标点，不吞并后续句子。"""
    text = "他说了一句话。晨光透过窗棂洒落在青石板上泛起淡金色的薄雾，他抬头。他继续走。"
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    pos = text.find(phrase)
    left, right = review_service._ngram_sentence_bounds(text, pos, len(phrase))
    sentence = text[left:right]
    assert sentence.endswith("他抬头。"), sentence
    assert "他继续走。" not in sentence, "不应吞并后续句子"


@pytest.mark.asyncio
async def test_ngram_rewrite_middle_sentence_no_tail_duplicate():
    """片段在句子中间 + LLM 返回完整句子 → 无双标点、无重复尾句、语句边界合理。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    # 注意：不能把 "结尾叙述。" * 20 放在相邻字面量拼接序列里（隐式拼接陷阱）
    content = (
        f"他推开门，{phrase}，然后转身坐下。\n"
        f"他再次进屋，{phrase}，然后关上门。\n"
    ) + "结尾叙述。" * 20
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    # LLM 返回完整句子（含句末标点）
    async def fake_chat(messages, **kwargs):
        return "他推开门，一缕微光从窗棂间漏进来，落在青石板上泛着淡金，他转身坐下了。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 替换后无双标点组合（。。/。，/，。 等）
    for bad in ("。。", "，。", "。，", "！。", "。！"):
        assert bad not in result, f"发现双标点 {bad}: ...{result[result.find(bad)-20:result.find(bad)+20]}..."
    # 无重复尾句（原片段不再出现）
    assert result.count(phrase) < content.count(phrase)
    # 语句边界合理：替换句以句号结尾
    rewritten_index = result.find("他推开门，一缕微光")
    assert rewritten_index >= 0
    segment_end = result.find("。", rewritten_index)
    assert segment_end >= 0
    assert result[segment_end + 1:segment_end + 2] == "\n", f"替换句后应紧跟换行: {repr(result[segment_end:segment_end+3])}"
    # 尾部保留
    assert result.endswith("结尾叙述。" * 20)


@pytest.mark.asyncio
async def test_ngram_rewrite_full_sentence_return_no_double_punct():
    """LLM 返回完整句子（自带句号）→ 替换整句后无双标点、无重复句尾。"""
    phrase = "他缓缓放下手中的信，看着窗外沉默了许久"
    # 注意：不能把 "收束。" * 10 放在相邻字面量拼接序列里（隐式拼接陷阱）
    content = (
        f"他走进房间，{phrase}，然后开口说话。\n"
        f"他想起昨夜，{phrase}，终于下了决心。\n"
    ) + "收束。" * 10
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    async def fake_chat(messages, **kwargs):
        return "他走进房间，轻轻搁下信纸，目光久久落在窗外，沉默了好一阵子才终于开口说话。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    for bad in ("。。", "，。", "。，", "。，然后", "，然后。"):
        assert bad not in result, f"发现双标点/残留: {bad}"
    assert result.count(phrase) < content.count(phrase)
    assert result.endswith("收束。" * 10)


@pytest.mark.asyncio
async def test_ngram_rewrite_long_unpunctuated_falls_back_to_fragment():
    """无标点超长段（超 MAX_NGRAM_SENTENCE_LEN）→ 退回片段替换，不吞并整段。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    # 无标点超长上下文（片段前后都是长无标点文本，句子边界会超限）
    long_context = "他站在窗前望着远方想着心事回忆起往昔的点点滴滴" * 30
    content = f"{long_context}{phrase}{long_context}\n" + f"{long_context}{phrase}{long_context}\n"
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    async def fake_chat(messages, **kwargs):
        return "改写后的片段文本内容，用于替换重复部分，保持语义一致但换一种表达方式呈现。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 只替换片段本身 → 上下文 long_context 不被吞并
    assert result.count(long_context) >= content.count(long_context), "无标点超长段不应被吞并"
    # 片段仍应减少（替换了首次出现）
    assert result.count(phrase) < content.count(phrase)


# ══════════════════════════════════════════════════════
# P2-2（Codex 五轮）：句界吸收闭合引号/省略号 + 超长段专用片段提示
# ══════════════════════════════════════════════════════

def test_ngram_sentence_bounds_absorbs_closing_quotes():
    """句末标点后的闭合引号应被吸收进句子（避免模型输出完整引号产生双引号）。"""
    text = "他低声道：“晨光透过窗棂洒落在青石板上泛起淡金色的薄雾，正是时候。”他说完就走。"
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    pos = text.find(phrase)
    left, right = review_service._ngram_sentence_bounds(text, pos, len(phrase))
    sentence = text[left:right]
    # 句子应含句号 + 结束引号
    assert sentence.endswith("。”"), f"应吸收结束引号: {sentence!r}"
    assert "他说完就走" not in sentence, "不应吞并后续句子"


def test_ngram_sentence_bounds_absorbs_ellipsis():
    """中文省略号（……）应被吸收为句子边界。"""
    text = "他望着窗外，晨光透过窗棂洒落在青石板上泛起淡金色的薄雾，渐渐入神……接着他回过神。"
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    pos = text.find(phrase)
    left, right = review_service._ngram_sentence_bounds(text, pos, len(phrase))
    sentence = text[left:right]
    assert sentence.endswith("……"), f"应吸收省略号: {sentence!r}"
    assert "接着他回过神" not in sentence


@pytest.mark.asyncio
async def test_ngram_rewrite_quoted_sentence_no_double_quote():
    """重复片段在带引号句子中 + 模型返回完整引号句 → 替换后无双引号。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    # 注意：不能把 "结尾。" * 10 放在相邻字面量拼接序列里（隐式拼接陷阱）
    content = (
        f"他低声道：“{phrase}，正是时候。”\n"
        f"他再次说：“{phrase}，该动身了。”\n"
    ) + "结尾。" * 10
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    # 模型返回完整引号句（含结束引号）
    async def fake_chat(messages, **kwargs):
        return "他低声道：“一缕微光从窗棂间漏进来，落在青石板上泛着淡金，正是时候。”"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 无双引号（不会出现 "" 或 ”” 相邻）
    assert '""' not in result
    assert "””" not in result
    # 结束引号只出现一次（每个引号句一次）
    assert result.count("”") <= content.count("”"), f"不应新增引号: {result}"
    assert result.count(phrase) < content.count(phrase)
    assert result.endswith("结尾。" * 10)


@pytest.mark.asyncio
async def test_ngram_rewrite_long_fragment_uses_fragment_prompt():
    """超长无标点段 → 使用"只输出片段"提示（user 内容是重复片段而非句子），且替换片段本身。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    long_context = "他站在窗前望着远方想着心事回忆起往昔的点点滴滴" * 30
    content = f"{long_context}{phrase}{long_context}\n" + f"{long_context}{phrase}{long_context}\n"
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return "改写后的片段文本内容，用于替换重复部分，保持语义一致但换一种表达方式呈现。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # 超长段走 fragment 提示
    assert "片段" in captured["system"], "超长段应使用片段提示"
    assert "重复片段：" in captured["user"], "user 内容应为「重复片段」而非「重复句子」"
    assert "重复句子：" not in captured["user"]
    # 片段仍减少、上下文不被吞并
    assert result.count(phrase) < content.count(phrase)
    assert result.count(long_context) >= content.count(long_context)


# ══════════════════════════════════════════════════════
# D2：复检回滚逻辑（Codex 三轮 P1）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rewrite_ngram_repeat_window_replaces_exact_span():
    """重写替换必须严格用窗口 (start,end)，不插入额外上下文（D1 坐标断言）。"""
    phrase = "晨光透过窗棂洒落在青石板上泛起淡金色的薄雾"
    prefix = "前文" * 300  # 600 字，>400 窗口
    suffix = "结尾" * 100
    content = f"{prefix}\n{phrase}，一。\n{phrase}，二。\n{suffix}"
    ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": phrase}],
        "score": 55,
    }

    async def fake_chat(messages, **kwargs):
        return "替换后的句子文本，保持语义一致但换一种表达。"

    with patch.object(review_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await review_service.rewrite_ngram_repeat(content, ai)

    # D1 核心：窗口只作参考，替换只针对句子 → 前缀完整保留（不重复插入/不截断）
    assert result.startswith(prefix)
    # 不能出现"前文"被重复插入（旧 bug 特征：前缀被写两次）
    assert result.count("前文") == content.count("前文"), f"前缀被重复插入: {result.count('前文')}"
    # 后缀完整保留
    assert result.endswith(suffix)


# ══════════════════════════════════════════════════════
# D2：复检回滚判定（Codex 三轮 P1）
# ══════════════════════════════════════════════════════

def test_should_accept_ngram_rewrite_true_when_clean_and_no_score_drop():
    """复检无高严重度重复 且 评分不下降 → 接受重写。"""
    original_ai = {"score": 55}
    recheck_ai = {"issues": [{"type": "句式重复", "severity": "medium"}], "score": 65}
    assert review_service._should_accept_ngram_rewrite(original_ai, recheck_ai) is True


def test_should_accept_ngram_rewrite_false_when_high_repeat_remains():
    """复检仍有高严重度同章原句重复 → 拒绝（回滚）。"""
    original_ai = {"score": 55}
    recheck_ai = {
        "issues": [{"type": "同章原句重复", "severity": "high", "original": "xx"}],
        "score": 65,
    }
    assert review_service._should_accept_ngram_rewrite(original_ai, recheck_ai) is False


def test_should_accept_ngram_rewrite_false_when_score_drops():
    """复检评分下降 → 拒绝（回滚），即使重复已消除。"""
    original_ai = {"score": 70}
    recheck_ai = {"issues": [], "score": 50}
    assert review_service._should_accept_ngram_rewrite(original_ai, recheck_ai) is False


def test_should_accept_ngram_rewrite_handles_missing_scores():
    """缺少 score 字段时安全处理（不抛异常）。"""
    original_ai = {}
    recheck_ai = {"issues": [], "score": 60}
    assert review_service._should_accept_ngram_rewrite(original_ai, recheck_ai) is True


# ══════════════════════════════════════════════════════
# Codex P1-3：跨章整段重复检测
# ══════════════════════════════════════════════════════

def test_cross_chapter_ngram_repeat_detects_long_duplicate():
    """当前章与前章出现 ≥20 字完全相同的连续文本 → 命中。"""
    prior = [
        "夜色如墨，青云宗山门前的石阶上，一道黑影缓缓走来。他停下脚步，抬头望向那轮残月，"
        "眼中闪过一丝决然。"
    ]
    current = (
        "他停下脚步，抬头望向那轮残月，眼中闪过一丝决然。"
        "随后他深吸一口气，推开了沉重的山门，踏入那片黑暗之中。"
    )
    found = review_service._cross_chapter_ngram_repeat(current, prior)
    assert found, "应检测到跨章重复"
    assert len(found[0]["phrase"]) >= review_service.AI_CROSS_NGRAM_MIN_LEN
    assert found[0]["prior_chapter_index"] == 0


def test_cross_chapter_ngram_repeat_high_len_is_long():
    """≥40 字整段复制 → length 足够判定 high。"""
    long_sentence = (
        "他缓缓抬起手，指尖凝聚起一缕幽蓝色的火焰，映照着他冷峻的面容，"
        "周围的空气仿佛都凝固了。"
    )
    prior = [f"前章内容。{long_sentence}后续继续。"]
    current = f"本章开头。{long_sentence}然后他转身离去。"
    found = review_service._cross_chapter_ngram_repeat(current, prior)
    assert found
    assert len(found[0]["phrase"]) >= review_service.AI_CROSS_NGRAM_HIGH_LEN


def test_cross_chapter_ngram_repeat_no_false_positive():
    """无重复 → 返回空列表。"""
    prior = ["前章讲的是宗门夜袭，弟子们奋力抵抗，最终击退了来犯之敌。"]
    current = "本章讲的是主角下山历练，在集市上遇到了一位神秘老者。"
    assert review_service._cross_chapter_ngram_repeat(current, prior) == []


def test_cross_chapter_ngram_repeat_empty_prior():
    """无前章文本 → 返回空列表。"""
    assert review_service._cross_chapter_ngram_repeat("任意正文内容", []) == []
    assert review_service._cross_chapter_ngram_repeat("任意正文内容", ["", "  "]) == []


def test_cross_chapter_ngram_repeat_allows_codes():
    """编号/专名等允许重复片段不误判。"""
    prior = ["编号 YJ-1963-0417 的卷宗被调出。"]
    current = "编号 YJ-1963-0417 的卷宗被调出，他仔细翻阅。"
    found = review_service._cross_chapter_ngram_repeat(current, prior)
    # 允许重复片段应被过滤（可能命中但被 _is_allowed_repeat 排除）
    for repeat in found:
        assert not review_service._is_allowed_repeat(repeat["phrase"])
