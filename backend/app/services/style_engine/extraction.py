"""M4 文风画像引擎 · 抽取模块。

职责：
1. 代表性采样（representative sampling）：按章节/段落分块均匀抽样，
   避免把整本稿子喂给 LLM 造成成本爆炸（PRD 明确约束）。
2. 客观统计（metrics）：平均句长 / 对话占比 / 描写密度 / 段落长短分布等纯规则统计，
   LLM 失败时也可独立产出（降级路径）。
3. 五维量化（dimensions）：把 metrics 对齐前端 STYLE_PRESETS 的五个维度
   （avgSentenceLength / dialogueRatio / descriptionDensity / paragraphLength / tone）。
4. 结构化抽取四组规则（叙述/角色/语言/节奏 + 反 AI 规则目录）：
   调用 LLM 从样本文本抽取；LLM 不可用时由规则统计降级生成。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.provider import LLMResponseError, llm
from app.llm.resolver import get_runtime_provider
from app.llm.roles import ROLE_STYLE

# ── 采样参数 ─────────────────────────────────────────────
# 全文低于此字符数时直接使用全文（避免过度抽样）
MAX_FULL_TEXT_CHARS = 24000
# 抽样总字符数上限（成本控制）
MAX_SAMPLE_CHARS = 20000
# 单章抽样字符上限
CHAPTER_SAMPLE_CHARS = 3000
# 章首取样字符数
CHAPTER_HEAD_CHARS = 1000
# 章尾取样字符数
CHAPTER_TAIL_CHARS = 600
# 章节较多时最多抽样的章数（均匀取点）
MAX_SAMPLED_CHAPTERS = 24
# 抽取特征数下限：四组规则合计少于该值视为不合格（需重试/降级）
MIN_EXTRACTED_RULES = 3

# 句子结束符与引号字符
SENTENCE_TERMINATORS = "。！？!?…；;"
QUOTE_CHARS = '“”"「」『』《》'

# 描写启发式线索词（纯规则统计用，非硬性标准）
DESCRIPTIVE_CUES = (
    "仿佛", "如同", "宛如", "似乎", "好像", "朦胧", "昏暗", "寂静", "凛冽",
    "阴沉", "明亮", "苍茫", "空旷", "蜿蜒", "斑驳", "幽深", "苍翠", "萧瑟",
    "微风", "阳光", "月光", "夜色", "山峦", "云雾", "细雨", "灯火", "薄雾",
    "空气", "味道", "香气", "光影", "天色", "天空", "大地", "尘埃", "余晖",
)


# ══════════════════════════════════════════════════════
# 采样
# ══════════════════════════════════════════════════════

def _build_full_text(chapters: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"第{item['order']}章 {item.get('title') or ''}\n{item['content']}"
        for item in chapters
    )


def _even_indices(count: int, total: int) -> list[int]:
    """在 [0, total) 上均匀取 count 个下标（含首尾）。"""
    if total <= 0:
        return []
    if count >= total:
        return list(range(total))
    return [round(i * (total - 1) / (count - 1)) for i in range(count)]


def sample_chapters(chapters: list[dict[str, Any]]) -> tuple[str, int, int, int]:
    """代表性采样：返回 (样本文本, 全文总字符数, 样本字符数, 抽样章数)。

    采样策略（按章节/段落均匀抽）：
    - 全文 ≤ MAX_FULL_TEXT_CHARS 时直接返回全文；
    - 否则均匀选取至多 MAX_SAMPLED_CHAPTERS 章，每章取「章首 + 章尾
      + 均匀取中间段落」，确保开头/中间/结尾均有代表。
    """
    full_text = _build_full_text(chapters)
    total = len(full_text)
    if total <= MAX_FULL_TEXT_CHARS:
        return full_text, total, total, len(chapters)

    selected_count = min(len(chapters), MAX_SAMPLED_CHAPTERS)
    selected = [chapters[i] for i in _even_indices(selected_count, len(chapters))]

    parts: list[str] = []
    sample_chars = 0
    for item in selected:
        content = item["content"]
        head = content[:CHAPTER_HEAD_CHARS]
        tail = content[-CHAPTER_TAIL_CHARS:] if len(content) > CHAPTER_HEAD_CHARS + CHAPTER_TAIL_CHARS else ""
        middle = _sample_middle_paragraphs(content, CHAPTER_SAMPLE_CHARS - len(head) - len(tail))
        block = head
        if middle:
            block += "\n……\n" + middle
        if tail:
            block += "\n……\n" + tail
        sample_chars += len(block)
        parts.append(f"第{item['order']}章 {item.get('title') or ''}\n{block}")

    return "\n\n".join(parts), total, sample_chars, len(selected)


def _sample_middle_paragraphs(content: str, budget: int) -> str:
    """在正文中段均匀抽取若干段落，返回拼接文本（不超预算）。"""
    if budget <= 0:
        return ""
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    middle = paragraphs[len(paragraphs) // 3: 2 * len(paragraphs) // 3]
    if not middle:
        return ""
    selected_count = min(len(middle), max(2, budget // 200))
    picked = [middle[i] for i in _even_indices(selected_count, len(middle))]
    out: list[str] = []
    used = 0
    for para in picked:
        if used + len(para) > budget:
            break
        out.append(para)
        used += len(para)
    return "\n".join(out)


# ══════════════════════════════════════════════════════
# 客观统计（零 LLM 成本）
# ══════════════════════════════════════════════════════

def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[。！？!?…；;]+", text) if p.strip()]


def _count_cjk_chars(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")


def _looks_descriptive(paragraph: str) -> bool:
    return any(cue in paragraph for cue in DESCRIPTIVE_CUES)


def compute_metrics(text: str) -> dict[str, Any]:
    """对（采样）文本做客观统计，产出可独立使用的指标字典。"""
    sentences = split_sentences(text)
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    total_chars = len(text)
    quote_chars = sum(1 for ch in text if ch in QUOTE_CHARS)

    descriptive_chars = sum(
        len(para) for para in paragraphs if _looks_descriptive(para)
    )
    non_dialogue_chars = max(total_chars - quote_chars, 1)

    total_sentences = len(sentences)
    total_paragraphs = len(paragraphs)

    short_para = sum(1 for p in paragraphs if len(p) < 40)
    long_para = sum(1 for p in paragraphs if len(p) > 120)
    medium_para = total_paragraphs - short_para - long_para

    short_sent = sum(1 for s in sentences if len(s) < 15)
    long_sent = sum(1 for s in sentences if len(s) > 30)

    return {
        "totalChars": total_chars,
        "totalSentences": total_sentences,
        "totalParagraphs": total_paragraphs,
        "wordCount": _count_cjk_chars(text),
        "avgSentenceLength": round(total_chars / total_sentences, 1) if total_sentences else 0.0,
        "dialogueRatio": round(quote_chars / total_chars, 3) if total_chars else 0.0,
        "descriptionDensity": round(descriptive_chars / non_dialogue_chars, 3) if non_dialogue_chars else 0.0,
        "paragraphShortRatio": round(short_para / total_paragraphs, 3) if total_paragraphs else 0.0,
        "paragraphMediumRatio": round(medium_para / total_paragraphs, 3) if total_paragraphs else 0.0,
        "paragraphLongRatio": round(long_para / total_paragraphs, 3) if total_paragraphs else 0.0,
        "sentenceShortRatio": round(short_sent / total_sentences, 3) if total_sentences else 0.0,
        "sentenceLongRatio": round(long_sent / total_sentences, 3) if total_sentences else 0.0,
    }


def _infer_tone(metrics: dict[str, Any]) -> str:
    """依据客观指标给一个粗粒度的语气标签（对齐 STYLE_PRESETS 中文命名）。"""
    avg = metrics.get("avgSentenceLength", 0) or 0
    dialog = metrics.get("dialogueRatio", 0) or 0
    desc = metrics.get("descriptionDensity", 0) or 0
    if avg and avg < 14 and dialog < 0.4:
        return "紧张凌厉"
    if avg and avg >= 24 and desc >= 0.5:
        return "诗意华丽"
    if dialog >= 0.45:
        return "幽默诙谐"
    if avg >= 26 and desc >= 0.55:
        return "史诗宏大"
    if desc >= 0.55:
        return "细腻温情"
    if avg < 18 and desc < 0.45:
        return "简洁冷峻"
    return "平稳克制"


def compute_dimensions(metrics: dict[str, Any]) -> dict[str, Any]:
    """把客观指标对齐前端 STYLE_PRESETS 的五维（avgSentenceLength/dialogueRatio/
    descriptionDensity/paragraphLength/tone）。"""
    short_r = metrics.get("paragraphShortRatio", 0) or 0
    long_r = metrics.get("paragraphLongRatio", 0) or 0
    if long_r > 0.5:
        paragraph_length = "long"
    elif short_r > 0.5:
        paragraph_length = "short"
    else:
        paragraph_length = "medium"
    return {
        "avgSentenceLength": round(metrics.get("avgSentenceLength", 0) or 0, 1),
        "dialogueRatio": round(metrics.get("dialogueRatio", 0) or 0, 2),
        "descriptionDensity": round(metrics.get("descriptionDensity", 0) or 0, 2),
        "paragraphLength": paragraph_length,
        "tone": _infer_tone(metrics),
    }


# ══════════════════════════════════════════════════════
# LLM 结构化抽取（自研 prompt，不复制参照项目）
# ══════════════════════════════════════════════════════

STYLE_EXTRACTION_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def build_extraction_prompt(sample_text: str, metrics: dict[str, Any]) -> str:
    metric_lines = [
        f"- 平均句长：{metrics.get('avgSentenceLength', 0)} 字",
        f"- 对话占比：{metrics.get('dialogueRatio', 0) * 100:.1f}%",
        f"- 描写密度：{metrics.get('descriptionDensity', 0) * 100:.1f}%",
        f"- 段落分布：短 {metrics.get('paragraphShortRatio', 0) * 100:.0f}% / "
        f"中 {metrics.get('paragraphMediumRatio', 0) * 100:.0f}% / "
        f"长 {metrics.get('paragraphLongRatio', 0) * 100:.0f}%",
    ]
    return (
        "以下是已按章节均匀抽样的小说正文节选，以及基于全稿样本的客观统计：\n\n"
        "【客观统计】\n" + "\n".join(metric_lines) + "\n\n"
        "【正文节选】\n" + sample_text[:18000] + "\n\n"
        "请基于节选与统计，严格按系统要求输出 JSON 规则画像。"
    )


def _extract_json_object(reply: str) -> dict[str, Any]:
    cleaned = reply.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMResponseError("文风抽取返回内容不是有效 JSON。")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"文风抽取 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMResponseError("文风抽取 JSON 顶层不是对象。")
    return data


def _normalize_rule_list(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise LLMResponseError("规则组不是数组。")
    rules: list[dict[str, str]] = []
    for item in items[:20]:
        if isinstance(item, str):
            rule = item.strip()
            key = ""
        elif isinstance(item, dict):
            rule = str(item.get("rule") or item.get("content") or "").strip()
            key = str(item.get("key") or "").strip()
        else:
            continue
        if rule:
            rules.append({"rule": rule, "key": key, "source": "llm"})
    return rules


def _build_report(
    extracted: dict[str, Any],
    metrics: dict[str, Any],
    degraded: bool = False,
) -> str:
    if degraded:
        return (
            "深度文风分析暂不可用（LLM 未配置或调用失败），已降级为规则统计模式。"
            "当前画像仅包含客观文本指标与通用建议；叙事/角色/用词特征待 LLM 可用后补充。"
        )
    dims = compute_dimensions(metrics)
    strengths = "；".join(extracted.get("strengths", [])[:3]) or "暂无"
    weaknesses = "；".join(extracted.get("weaknesses", [])[:3]) or "暂无"
    return (
        f"现状画像：平均句长约 {dims['avgSentenceLength']} 字、对话占比约 "
        f"{dims['dialogueRatio'] * 100:.0f}%、描写密度约 {dims['descriptionDensity'] * 100:.0f}%、"
        f"段落以{dims['paragraphLength']}为主。"
        f"优点：{strengths}。改进方向：{weaknesses}。"
    )


def parse_rules_json(reply: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """解析 LLM 返回的结构化画像；不合格（特征过少/缺组）抛 LLMResponseError。"""
    data = _extract_json_object(reply)
    result: dict[str, Any] = {}
    for key in ("narrative_rules", "character_rules", "language_rules", "rhythm_rules", "anti_ai_rules"):
        result[key] = _normalize_rule_list(data.get(key))

    total = sum(len(result[k]) for k in ("narrative_rules", "character_rules", "language_rules", "rhythm_rules"))
    if total < MIN_EXTRACTED_RULES:
        raise LLMResponseError(f"文风抽取特征过少（{total} 条），视为不合格。")

    strengths = data.get("strengths") or []
    weaknesses = data.get("weaknesses") or []
    result["strengths"] = [str(x).strip() for x in strengths if str(x).strip()][:10]
    result["weaknesses"] = [str(x).strip() for x in weaknesses if str(x).strip()][:10]
    result["report"] = _build_report(result, metrics)
    return result


async def extract_rules(sample_text: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """调用 LLM 抽取四组规则；LLM 层异常向上抛，由编排层降级。"""
    messages = [
        {"role": "system", "content": STYLE_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": build_extraction_prompt(sample_text, metrics)},
    ]
    reply = await get_runtime_provider(llm).chat(messages, role=ROLE_STYLE, temperature=0.3)
    return parse_rules_json(reply, metrics)


# ══════════════════════════════════════════════════════
# 降级路径：纯规则统计生成规则画像（零 LLM 成本）
# ══════════════════════════════════════════════════════

def build_rules_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """LLM 不可用时，由客观指标推导一组通用规则画像（status='degraded'）。"""
    avg = metrics.get("avgSentenceLength", 0) or 0
    dialog = metrics.get("dialogueRatio", 0) or 0
    desc = metrics.get("descriptionDensity", 0) or 0
    short_r = metrics.get("paragraphShortRatio", 0) or 0
    long_r = metrics.get("paragraphLongRatio", 0) or 0

    rhythm_notes = []
    if avg and avg < 18:
        rhythm_notes.append("以短句为主，节奏偏快")
    elif avg and avg > 24:
        rhythm_notes.append("以长句为主，节奏舒缓")
    if dialog >= 0.45:
        rhythm_notes.append("对话占比较高，叙事推进依赖对话")
    elif dialog <= 0.2:
        rhythm_notes.append("叙述占主导，对话使用克制")
    if short_r > 0.5:
        rhythm_notes.append("段落以短段为主，视觉节奏快")
    elif long_r > 0.5:
        rhythm_notes.append("段落以长段为主，绵密铺陈")

    narrative_rules = [
        {
            "key": "avg_sentence_length",
            "rule": f"全文平均句长约 {avg} 字，整体{('以短句为主' if avg and avg < 18 else ('以中长句为主' if avg and avg > 24 else '长短句均衡'))}，"
            f"{('节奏紧凑' if avg and avg < 18 else ('节奏舒缓' if avg and avg > 24 else '节奏平稳'))}。",
            "source": "metric",
        },
        {
            "key": "paragraph_flow",
            "rule": f"段落{('偏短' if short_r > 0.5 else ('偏长' if long_r > 0.5 else '长短均衡'))}，"
            f"{('适合快速推进、留白多' if short_r > 0.5 else ('适合细腻铺陈' if long_r > 0.5 else '兼顾推进与铺陈'))}。",
            "source": "metric",
        },
    ]
    rhythm_rules = [
        {
            "key": "dialogue_ratio",
            "rule": f"对话占全文约 {dialog * 100:.0f}%，" + ("对话与叙述并重" if 0.2 < dialog < 0.45 else ("偏对话" if dialog >= 0.45 else "偏叙述")) + "。",
            "source": "metric",
        },
    ] + [
        {"key": f"rhythm_{i}", "rule": note, "source": "metric"}
        for i, note in enumerate(rhythm_notes, 1)
    ]
    language_rules = [
        {
            "key": "description_density",
            "rule": f"描写密度约 {desc * 100:.0f}%，环境与感官描写{('较丰富' if desc >= 0.5 else '较克制')}。",
            "source": "metric",
        },
    ]
    character_rules: list[dict[str, str]] = []
    anti_ai_rules = [
        {"key": "avoid_repetitive_openings", "rule": "避免连续章节以相同句式或相同场景开头。", "source": "metric"},
        {"key": "avoid_connective_overuse", "rule": "避免过度使用「仿佛/然而/顿时/突然/不由得」等连接词。", "source": "metric"},
        {"key": "avoid_flat_rhythm", "rule": "避免句长与段落长度长期单调，按场景制造长短波动。", "source": "metric"},
        {"key": "avoid_vague_emotion", "rule": "避免用「心情复杂」「百感交集」等空泛表达替代具体动作与细节。", "source": "metric"},
    ]
    strengths = [
        f"平均句长约 {avg} 字，节奏基调已量化",
        f"对话占比约 {dialog * 100:.0f}%，叙述与对话比例已量化",
        f"段落长短分布已量化（短 {short_r * 100:.0f}% / 长 {long_r * 100:.0f}%）",
    ]
    weaknesses = [
        "未完成 LLM 深度分析，暂无叙事偏好/角色语言/用词习惯特征",
        "如需更精确的规则画像（角色说话方式、反 AI 目录等），请配置 LLM 后重新分析",
    ]
    return {
        "narrative_rules": narrative_rules,
        "character_rules": character_rules,
        "language_rules": language_rules,
        "rhythm_rules": rhythm_rules,
        "anti_ai_rules": anti_ai_rules,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "report": _build_report({}, metrics, degraded=True),
    }
