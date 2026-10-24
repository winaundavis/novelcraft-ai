"""Language and logic review services with ASCII-safe fallbacks."""

import json
import re
from typing import Any, Callable

from app.llm.audit import reset_llm_audit_context, set_llm_audit_context
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.resolver import get_runtime_provider
from app.utils.text_repair import repair_text

REVIEW_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


async def review_language(text: str, chapter_id: str = "", allow_llm_call: Callable[[str, int], bool] | None = None) -> dict:
    if not text.strip():
        return {"issues": [], "summary": "文本为空，无需审校。"}

    if allow_llm_call and not allow_llm_call("review", 900):
        return {"issues": [], "summary": "budget exhausted", "budget_exhausted": True}
    token = set_llm_audit_context(role="check")
    try:
        reply = await get_runtime_provider(llm).chat(
            [
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "只审校语言质量，返回 JSON。\n"
                        f"Chapter ID: {chapter_id or 'not provided'}\n\n"
                        f"Text:\n{text[:5000]}"
                    ),
                },
            ]
        )
        return _parse_review_result(reply, "language", text)
    except Exception:
        return _heuristic_review("language", text)
    finally:
        reset_llm_audit_context(token)


async def review_logic(
    text: str,
    chapter_id: str = "",
    characters: list | None = None,
    world_rules: list | None = None,
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> dict:
    if not text.strip():
        return {"issues": [], "summary": "文本为空，无需审校。"}

    context_lines = []
    for character in (characters or [])[:8]:
        # 审校只看“人物行为是否前后一致”，所以取动机、欲望、价值观与声线这四项。
        segments = [f"Character: {character.get('name', '')}"]
        for label, key in (
            ("Motivation", "surfaceMotivation"),
            ("Desire", "deepDesire"),
            ("Values", "values"),
            ("Speech", "speechStyle"),
        ):
            value = str(character.get(key) or "").strip()
            if value:
                segments.append(f"{label}: {value}")
        context_lines.append(" / ".join(segments))
    for rule in (world_rules or [])[:10]:
        context_lines.append(f"Rule: {rule.get('name', '')} / {rule.get('description', '')}")

    context_text = "\n".join(context_lines) if context_lines else "None"

    if allow_llm_call and not allow_llm_call("review", 900):
        return {"issues": [], "summary": "budget exhausted", "budget_exhausted": True}
    token = set_llm_audit_context(role="check")
    try:
        reply = await get_runtime_provider(llm).chat(
            [
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "只审校情节逻辑、因果关系、一致性和世界规则，返回 JSON。\n"
                        f"Chapter ID: {chapter_id or 'not provided'}\n\n"
                        f"Context:\n{context_text}\n\n"
                        f"Text:\n{text[:5000]}"
                    ),
                },
            ]
        )
        return _parse_review_result(reply, "logic", text)
    except Exception:
        return _heuristic_review("logic", text)
    finally:
        reset_llm_audit_context(token)


async def review_chapter_comprehensive(
    text: str,
    chapter_id: str = "",
    characters: list | None = None,
    world_rules: list | None = None,
) -> dict:
    language_result = await review_language(text, chapter_id)
    logic_result = await review_logic(text, chapter_id, characters, world_rules)
    rhythm_result = _analyze_rhythm(text)

    issues = [
        *language_result.get("issues", []),
        *logic_result.get("issues", []),
        *rhythm_result.get("issues", []),
    ]
    severity_order = {"fatal": 0, "important": 1, "suggestion": 2}
    issues.sort(key=lambda item: severity_order.get(item.get("severity", "suggestion"), 9))

    fatal_count = sum(1 for item in issues if item.get("severity") == "fatal")
    important_count = sum(1 for item in issues if item.get("severity") == "important")
    suggestion_count = sum(1 for item in issues if item.get("severity") == "suggestion")

    return {
        "totalIssues": len(issues),
        "fatalCount": fatal_count,
        "importantCount": important_count,
        "suggestionCount": suggestion_count,
        "issues": issues,
        "summary": (
            f"综合审校完成：致命 {fatal_count}，重要 {important_count}，建议 {suggestion_count}。"
        ),
        "languageScore": _calc_score(language_result),
        "logicScore": _calc_score(logic_result),
        "rhythmScore": rhythm_result.get("score", 85),
    }


def _parse_review_result(reply: str, review_type: str, text: str) -> dict:
    try:
        start = reply.find("{")
        end = reply.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(reply[start:end])
            if isinstance(data, dict) and "issues" in data:
                data.setdefault("summary", "审校完成。")
                return data
    except (json.JSONDecodeError, ValueError):
        pass

    heuristic = _heuristic_review(review_type, text)
    if heuristic["issues"]:
        return heuristic
    raise LLMResponseError("审校返回无法解析为结构化问题。")


def _heuristic_review(review_type: str, text: str) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    paragraphs = [item.strip() for item in text.split("\n\n") if item.strip()]

    if review_type == "language":
        repeated_openers: dict[str, int] = {}
        for index, paragraph in enumerate(paragraphs[:10], start=1):
            opener = paragraph[:24]
            repeated_openers[opener] = repeated_openers.get(opener, 0) + 1

            if paragraph.lower().count("said") >= 2:
                issues.append(
                    {
                        "severity": "suggestion",
                        "type": "重复的对话标签",
                        "location": f"第 {index} 段",
                        "original": paragraph[:80],
                        "suggestion": "用动作描写或内心反应替换重复的对话标签。",
                        "reason": "重复的对话标签会削弱句式的丰富性。",
                    }
                )

        for opener, count in repeated_openers.items():
            if opener and count >= 3:
                issues.append(
                    {
                        "severity": "important",
                        "type": "段落开头重复",
                        "location": "开头段落",
                        "original": opener,
                        "suggestion": "变化主语焦点或改写开头，改善节奏。",
                        "reason": "重复的开头会削弱节奏和叙事质感。",
                    }
                )
                break
    else:
        if text.lower().count("suddenly") >= 3:
            issues.append(
                {
                    "severity": "suggestion",
                    "type": "\"突然\"使用过度",
                    "location": "全文",
                    "original": "suddenly",
                    "suggestion": "减少重复的突兀转折词，使用更清晰的因果过渡。",
                    "reason": "过多突兀转折会削弱因果链条。",
                }
            )

    return {
        "issues": issues,
        "summary": "本地启发式审校完成。" if issues else "本地启发式审校未发现明显问题。",
    }


def _analyze_rhythm(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"score": 85, "issues": []}

    paragraphs = [item for item in text.split("\n\n") if item.strip()]
    normalized = re.sub(r"[!?]", ".", text)
    sentences = [item.strip() for item in normalized.split(".") if item.strip()]

    issues = []
    avg_paragraph_length = sum(len(item) for item in paragraphs) / max(len(paragraphs), 1)
    if avg_paragraph_length > 500:
        issues.append(
            {
                "severity": "suggestion",
                "type": "段落过长",
                "location": "全文",
                "original": f"平均段落长度：{int(avg_paragraph_length)} 字符",
                "suggestion": "将段落拆分为更小的段落，改善阅读呼吸感和浏览节奏。",
                "reason": "过长的段落通常会让节奏扁平化。",
            }
        )

    avg_sentence_length = sum(len(item) for item in sentences) / max(len(sentences), 1)
    if avg_sentence_length > 40:
        issues.append(
            {
                "severity": "suggestion",
                "type": "句子平均过长",
                "location": "全文",
                "original": f"平均句子长度：{int(avg_sentence_length)} 字符",
                "suggestion": "在高张力段落中引入更短的句子。",
                "reason": "不间断的长句会降低张力对比度。",
            }
        )

    score = 90 - min(len(issues) * 5, 15)
    return {
        "score": max(score, 50),
        "issues": issues,
        "avgParagraphLen": int(avg_paragraph_length),
        "avgSentenceLen": int(avg_sentence_length),
        "paragraphCount": len(paragraphs),
        "sentenceCount": len(sentences),
    }


def _calc_score(review_result: dict) -> int:
    score = 100
    for issue in review_result.get("issues", []):
        severity = issue.get("severity", "")
        if severity == "fatal":
            score -= 20
        elif severity == "important":
            score -= 10
        else:
            score -= 3
    return max(score, 30)


# ══════════════════════════════════════════════════════
# M3 AI 味检测（确定性规则，零 LLM 成本）
#
# 对应 PRD M3-③：确定性规则 —— 开头 30 字重复、句式重复率、连接词滥用、
# 场景关键词组。命中即标记「质量债」（记录到任务统计），超阈值触发局部重写。
# 参照项目二 quality guards 的 audit_chapter_continuity（关键词组跨章匹配 +
# 开头段落重复检测），但 prompt/文案全部自研。
# ══════════════════════════════════════════════════════

AI_OPENING_REPEAT_LENGTH = 30       # 开头重复检测窗口（前 30 字）
AI_OPENING_REPEAT_RATIO = 0.7       # 与上章开头 30 字相似度 ≥0.7 → high（触发局部重写）
AI_SENTENCE_OPENING_LEN = 4         # 句式重复检测：句子前 4 字
AI_SENTENCE_MIN_COUNT = 8           # 少于 8 句不检测句式重复（样本过小）
AI_SENTENCE_REPEAT_RATIO_HIGH = 0.5  # ≥50% 句子开头重复 → high（触发局部重写）
AI_SENTENCE_REPEAT_RATIO_MED = 0.35  # ≥35% → medium（质量债）
AI_CONNECTIVE_WORDS = (
    # 只收录「AI 高频模糊词 / 万能副词」。逻辑转折词（但是 / 然而）是人类写作的正常连接，
    # 算进来只会误报（2026-10-20 网文创作评审结论），故移除。
    "突然", "仿佛", "似乎", "像是", "宛如", "隐约",
    "缓缓", "轻轻", "竟然", "瞬间", "不由得", "顿时",
)
AI_CONNECTIVE_PER_500_HIGH = 6.0    # 每 500 字 ≥6 个连接词 → medium
AI_CONNECTIVE_PER_500_MED = 4.0     # 每 500 字 ≥4 个 → low
AI_SCENE_CLUSTERS = (
    ("深夜", "独自", "沉默", "月光"),
    ("目光", "对视", "沉默", "呼吸"),
    ("皱眉", "沉思", "抬头", "窗外"),
    ("再次", "缓缓", "仿佛", "一切"),
)
AI_SCENE_CLUSTER_HIT = 3            # 同组 ≥3 词在前后两章同时出现 → 命中

# 同章原句重复检测（验收 P1：AI 味检测盲区 —— 同章内 n-gram 原样重复）
AI_NGRAM_MIN_LEN = 10              # 连续子串最短长度（<10 字太短，正常行文也会重复）
AI_NGRAM_MIN_OCCURS = 2            # 至少出现 2 次才构成重复
AI_NGRAM_MAX_ISSUES = 3            # 最多报 3 处（避免刷屏）
AI_NGRAM_SCORE_PER_HIT = 5         # 每处重复扣分
AI_NGRAM_HIGH_LEN = 20             # ≥20 字完整句/段复制 → high（触发自动重写）；否则 medium
# 允许重复的片段特征：编号（YJ-1963-0417）/ 纯数字 / 短引用（含书名号/引号）
AI_NGRAM_ALLOWED_RE = (
    r"[A-Za-z]{0,6}-\d{2,}-\d{2,}",   # 编号 YJ-1963-0417
    r"[\d＃#]{3,}",                    # 纯数字/编号 ≥3 位
    r"《[^》]{1,12}》",                 # 书名/剧名等专名
)

# 跨章整段重复检测（Codex P1-3：正文质量未达标 —— 17 组整段完全重复）
# 长跑实测第 1 卷第 2、3 章大量相同情节、整段完全重复。此前 detect_ai_artifacts
# 只比对上一章开头 30 字 + 同章 n-gram，测不到「跨章整段复制」。
# 规则：当前章与**之前所有已落库章节**比对，≥AI_CROSS_NGRAM_MIN_LEN 字的连续
# 子串原样出现在任一前章 → 命中。≥AI_CROSS_NGRAM_HIGH_LEN 视为 high（阻止章节
# 通过，触发重写）；否则 medium（质量债）。
AI_CROSS_NGRAM_MIN_LEN = 20        # 跨章重复最短长度（≥20 字才算整句/整段复制）
AI_CROSS_NGRAM_HIGH_LEN = 40       # ≥40 字整段复制 → high（阻止通过）
AI_CROSS_NGRAM_MAX_ISSUES = 5      # 最多报 5 处
AI_CROSS_NGRAM_MAX_PRIOR = 8       # 最多比对前 8 章（控制成本，足够覆盖近期重复）
AI_CROSS_NGRAM_SCORE_PER_HIT = 8   # 每处跨章重复扣分


def _strip_ai_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _opening_similarity(a: str, b: str) -> float:
    """开头 30 字相似度（去空白后字符集合重合率，0.0-1.0）。"""
    a = _strip_ai_text(a)[:AI_OPENING_REPEAT_LENGTH]
    b = _strip_ai_text(b)[:AI_OPENING_REPEAT_LENGTH]
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    return len(set_a & set_b) / max(len(set_a), 1)


def _split_ai_sentences(text: str) -> list[str]:
    normalized = re.sub(r"[！？!?；;\n]", "。", text or "")
    return [item.strip() for item in normalized.split("。") if item.strip()]


def _sentence_opening_repetition(text: str) -> float:
    """句子开头重复率：出现 ≥2 次的句子前 4 字 占句子总数的比例。"""
    sentences = _split_ai_sentences(text)
    if len(sentences) < AI_SENTENCE_MIN_COUNT:
        return 0.0
    openings = [s[:AI_SENTENCE_OPENING_LEN] for s in sentences]
    counts: dict[str, int] = {}
    for opening in openings:
        counts[opening] = counts.get(opening, 0) + 1
    repeated = sum(1 for count in counts.values() if count >= 2)
    return repeated / len(sentences)


def _connective_overuse(text: str) -> float:
    """连接词密度：每 500 字命中 AI 连接词的次数。"""
    stripped = _strip_ai_text(text)
    if not stripped:
        return 0.0
    units = max(len(stripped) / 500.0, 1.0)
    total = sum(stripped.count(word) for word in AI_CONNECTIVE_WORDS)
    return total / units


def _scene_cluster_hit(text: str, previous_text: str) -> dict | None:
    """场景关键词组跨章匹配：前文与本章同时出现同一组 ≥3 个关键词。"""
    if not previous_text or not previous_text.strip():
        return None
    cur = _strip_ai_text(text)
    prev = _strip_ai_text(previous_text)
    for cluster in AI_SCENE_CLUSTERS:
        hit_cur = sum(1 for word in cluster if word in cur)
        hit_prev = sum(1 for word in cluster if word in prev)
        if hit_cur >= AI_SCENE_CLUSTER_HIT and hit_prev >= AI_SCENE_CLUSTER_HIT:
            return {"cluster": cluster, "cur_hit": hit_cur, "prev_hit": hit_prev}
    return None


def _is_allowed_repeat(phrase: str) -> bool:
    """允许重复的片段：编号/专名/短引用（Codex C3：YJ-1963-0417 不应误判）。

    这些内容在同一章重复出现是正常的（引用同一编号/书名），不是 AI 味。
    """
    if not phrase:
        return True
    # 纯数字 / 编号（YJ-1963-0417 等）
    for pattern in AI_NGRAM_ALLOWED_RE:
        if re.search(pattern, phrase):
            return True
    return False


def _same_chapter_ngram_repeat(text: str) -> list[dict]:
    """同章 n-gram 原句重复检测（验收 P1：AI 味检测盲区修复）。

    检测同章内**完全相同**的连续子串（长度 ≥ AI_NGRAM_MIN_LEN）出现 ≥2 次，
    如"晨光透过窗棂洒在地上"整句原样重复两遍。这类重复是 AI 生成的典型
    痕迹（模板句被复制粘贴），且此前 detect_ai_artifacts 的句式/开头/连接词
    规则都测不到。

    算法：去空白后，先统计所有长度 == min_len 的子串出现次数，找出重复种子；
    对每个种子取其两处出现位置，向两侧同步扩展成最长公共重复片段，得到
    完整的"原句重复"证据。返回 [{phrase, count}]，最多 AI_NGRAM_MAX_ISSUES 处。
    """
    stripped = _strip_ai_text(text)
    if len(stripped) < AI_NGRAM_MIN_LEN * 2:
        return []
    min_len = AI_NGRAM_MIN_LEN

    # 1) 找出重复的 min_len 种子
    seed_counts: dict[str, int] = {}
    for i in range(len(stripped) - min_len + 1):
        seed = stripped[i : i + min_len]
        seed_counts[seed] = seed_counts.get(seed, 0) + 1
    repeated_seeds = {
        seed for seed, n in seed_counts.items() if n >= AI_NGRAM_MIN_OCCURS
    }
    if not repeated_seeds:
        return []

    # 2) 对每个重复种子：取两处位置，双向扩展成最长公共片段
    found: list[dict] = []
    seen_phrases: set[str] = set()
    used_spans: list[tuple[int, int]] = []

    for seed in sorted(repeated_seeds, key=len, reverse=True):
        # 该种子所有出现位置
        positions: list[int] = []
        start = 0
        while True:
            idx = stripped.find(seed, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1
        if len(positions) < AI_NGRAM_MIN_OCCURS:
            continue
        # 用前两处位置扩展
        p1, p2 = positions[0], positions[1]
        # 跳过已被更大片段覆盖的锚点
        if any(s <= p1 < e for s, e in used_spans):
            continue
        # 向右同步扩展（两位置字符一致才扩）
        left, right = p1, p1 + len(seed)
        while right < len(stripped):
            offset = right - p1
            if p2 + offset >= len(stripped):
                break
            if stripped[right] == stripped[p2 + offset]:
                right += 1
            else:
                break
        # 向左同步扩展
        while left > 0:
            offset = p1 - left
            if p2 - offset - 1 < 0:
                break
            if stripped[left - 1] == stripped[p2 - offset - 1]:
                left -= 1
            else:
                break
        phrase = stripped[left:right]
        if len(phrase) < AI_NGRAM_MIN_LEN or phrase in seen_phrases:
            continue
        # 统计该片段全文出现次数
        occur = 0
        pos = 0
        while True:
            idx = stripped.find(phrase, pos)
            if idx < 0:
                break
            occur += 1
            pos = idx + 1
        if occur < AI_NGRAM_MIN_OCCURS:
            continue
        seen_phrases.add(phrase)
        used_spans.append((p1, right))
        found.append({"phrase": phrase, "count": occur, "positions": [p1, p2]})
        if len(found) >= AI_NGRAM_MAX_ISSUES:
            break
    return found


def _cross_chapter_ngram_repeat(text: str, prior_texts: list[str]) -> list[dict]:
    """跨章整段/长句重复检测（Codex P1-3：正文质量未达标）。

    检测当前章与**之前所有已落库章节**之间**完全相同**的连续子串
    （长度 ≥ AI_CROSS_NGRAM_MIN_LEN）。长跑实测第 1 卷第 2、3 章大量相同情节、
    整段完全重复——这类重复是模型在长上下文下复读前文导致的，且此前
    detect_ai_artifacts 只比对上一章开头 30 字 + 同章 n-gram，测不到。

    算法：对每个前章文本，取当前章去空白后的每个 ≥min_len 子串在前章中查找；
    命中则双向扩展成最长公共片段。返回 [{phrase, count, prior_chapter_index}]，
    最多 AI_CROSS_NGRAM_MAX_ISSUES 处。prior_texts 为空或过短时返回 []。
    """
    stripped = _strip_ai_text(text)
    if len(stripped) < AI_CROSS_NGRAM_MIN_LEN:
        return []
    prior_stripped = [
        _strip_ai_text(pt) for pt in prior_texts if pt and len(_strip_ai_text(pt)) >= AI_CROSS_NGRAM_MIN_LEN
    ]
    if not prior_stripped:
        return []

    found: list[dict] = []
    seen_phrases: set[str] = set()
    # 用当前章每个 min_len 种子在前章中查找（种子去重，避免重复扫描）
    min_len = AI_CROSS_NGRAM_MIN_LEN
    seeds = set(stripped[i : i + min_len] for i in range(len(stripped) - min_len + 1))

    for seed in seeds:
        if len(found) >= AI_CROSS_NGRAM_MAX_ISSUES:
            break
        # 在前章中找该种子
        hit_prior = -1
        hit_pos = -1
        for pi, prior in enumerate(prior_stripped):
            idx = prior.find(seed)
            if idx >= 0:
                hit_prior = pi
                hit_pos = idx
                break
        if hit_prior < 0:
            continue
        prior = prior_stripped[hit_prior]
        # 双向扩展成最长公共片段
        left, right = hit_pos, hit_pos + len(seed)
        # 当前章中种子位置
        cur_pos = stripped.find(seed)
        # 向右扩展
        while right < len(prior):
            offset = right - hit_pos
            if cur_pos + offset >= len(stripped):
                break
            if prior[right] == stripped[cur_pos + offset]:
                right += 1
            else:
                break
        # 向左扩展
        while left > 0:
            offset = hit_pos - left
            if cur_pos - offset - 1 < 0:
                break
            if prior[left - 1] == stripped[cur_pos - offset - 1]:
                left -= 1
            else:
                break
        phrase = prior[left:right]
        if len(phrase) < AI_CROSS_NGRAM_MIN_LEN or phrase in seen_phrases:
            continue
        if _is_allowed_repeat(phrase):
            continue
        seen_phrases.add(phrase)
        found.append(
            {
                "phrase": phrase,
                "count": 1,
                "prior_chapter_index": hit_prior,
                "length": len(phrase),
            }
        )
    # 按长度降序（最长的重复最严重）
    found.sort(key=lambda x: x["length"], reverse=True)
    return found[:AI_CROSS_NGRAM_MAX_ISSUES]


def detect_ai_artifacts(text: str, previous_text: str = "") -> dict:
    """M3 确定性 AI 味检测（零 LLM 成本）。

    规则：
      1. 开头 30 字与上一章重复（相似度 ≥0.7）→ high
      2. 句式重复率 ≥0.5 → high；≥0.35 → medium
      3. 连接词滥用：每 500 字 ≥6 → medium；≥4 → low
      4. 场景关键词组跨章同时出现 ≥3 词 → medium

    返回：
      {
        "issues": 详细问题列表（供局部重写提示使用）,
        "debt_items": 质量债清单（命中即记入任务统计）,
        "needs_rewrite": 是否超阈值触发局部重写（存在 high 项）,
        "score": 0-100 分,
        "opening": 本章开头 30 字,
        "previous_opening": 上一章开头 30 字,
      }
    """
    if not text or not text.strip():
        return {
            "issues": [], "debt_items": [], "needs_rewrite": False,
            "score": 100, "opening": "", "previous_opening": "",
        }

    issues: list[dict] = []
    debt_items: list[dict] = []

    cur_opening = _strip_ai_text(text)[:AI_OPENING_REPEAT_LENGTH]
    prev_opening = _strip_ai_text(previous_text)[:AI_OPENING_REPEAT_LENGTH]
    opening_sim = _opening_similarity(text, previous_text)
    if prev_opening and opening_sim >= AI_OPENING_REPEAT_RATIO:
        issues.append(
            {
                "severity": "high",
                "type": "开头重复",
                "location": "章节开头",
                "original": cur_opening,
                "suggestion": "改写开头，换一种进入场景的方式，避免与上一章雷同。",
                "reason": f"开头 30 字与上一章相似度 {opening_sim:.0%}，AI 味明显。",
            }
        )
        debt_items.append(
            {"type": "opening_repeat", "severity": "high",
             "detail": f"开头30字与上章相似度 {opening_sim:.0%}"}
        )

    sentence_ratio = _sentence_opening_repetition(text)
    if sentence_ratio >= AI_SENTENCE_REPEAT_RATIO_HIGH:
        issues.append(
            {
                "severity": "high",
                "type": "句式重复",
                "location": "全文",
                "original": f"句子开头重复率 {sentence_ratio:.0%}",
                "suggestion": "打散相同开头的句子，交替使用主语前置/状语前置/短句。",
                "reason": "大量句子以相同方式开头，句式单调，AI 味重。",
            }
        )
        debt_items.append(
            {"type": "sentence_repeat", "severity": "high",
             "detail": f"句子开头重复率 {sentence_ratio:.0%}"}
        )
    elif sentence_ratio >= AI_SENTENCE_REPEAT_RATIO_MED:
        issues.append(
            {
                "severity": "medium",
                "type": "句式重复",
                "location": "全文",
                "original": f"句子开头重复率 {sentence_ratio:.0%}",
                "suggestion": "适当打散句子开头，增加节奏变化。",
                "reason": "句子开头重复率偏高，节奏略显单调。",
            }
        )
        debt_items.append(
            {"type": "sentence_repeat", "severity": "medium",
             "detail": f"句子开头重复率 {sentence_ratio:.0%}"}
        )

    connective_density = _connective_overuse(text)
    if connective_density >= AI_CONNECTIVE_PER_500_HIGH:
        issues.append(
            {
                "severity": "medium",
                "type": "连接词滥用",
                "location": "全文",
                "original": f"每 500 字 {connective_density:.1f} 个连接词",
                "suggestion": "删除冗余的\"但是/然而/突然/仿佛\"等，改用动作与因果推进。",
                "reason": "AI 高频连接词使用密度过高，削弱叙事质感。",
            }
        )
        debt_items.append(
            {"type": "connective_overuse", "severity": "medium",
             "detail": f"每500字出现 {connective_density:.1f} 个 AI 连接词"}
        )
    elif connective_density >= AI_CONNECTIVE_PER_500_MED:
        issues.append(
            {
                "severity": "low",
                "type": "连接词滥用",
                "location": "全文",
                "original": f"每 500 字 {connective_density:.1f} 个连接词",
                "suggestion": "留意\"但是/然而/突然\"等连接词的使用密度。",
                "reason": "连接词使用密度略偏高。",
            }
        )
        debt_items.append(
            {"type": "connective_overuse", "severity": "low",
             "detail": f"每500字出现 {connective_density:.1f} 个 AI 连接词"}
        )

    cluster = _scene_cluster_hit(text, previous_text)
    if cluster:
        cluster_text = "/".join(cluster["cluster"])
        issues.append(
            {
                "severity": "medium",
                "type": "场景关键词组重复",
                "location": "跨章",
                "original": cluster_text,
                "suggestion": "更换场景氛围关键词组合，避免与上一章使用同一套场景描写。",
                "reason": f"与上一章同时出现关键词组（{cluster_text}），场景套路重复。",
            }
        )
        debt_items.append(
            {"type": "scene_cluster", "severity": "medium",
             "detail": f"与上章同时出现关键词组：{cluster_text}"}
        )

    # 同章 n-gram 原句重复（验收 P1：AI 味检测盲区修复）
    ngram_repeats = _same_chapter_ngram_repeat(text)
    for repeat in ngram_repeats:
        phrase = repeat["phrase"]
        count = repeat["count"]
        # 允许重复规则（Codex C3）：编号/专名/短引用 不应判为 AI 味
        if _is_allowed_repeat(phrase):
            continue
        # 严重度：≥20 字的完整句/段复制 → high（触发自动重写）；10-19 字 → medium
        is_long = len(phrase) >= AI_NGRAM_HIGH_LEN
        severity = "high" if is_long else "medium"
        issues.append(
            {
                "severity": severity,
                "type": "同章原句重复",
                "location": "同章",
                "original": phrase,
                "suggestion": "删除重复出现的原句，改写其中一处，避免同一句在章内原样出现两次。",
                "reason": f"同章内\"{phrase[:20]}{'…' if len(phrase) > 20 else ''}\"原样重复出现 {count} 次，AI 味明显。",
                "positions": repeat.get("positions", []),
            }
        )
        debt_items.append(
            {"type": "same_chapter_ngram_repeat", "severity": severity,
             "detail": f"同章原句重复 {count} 次：{phrase[:30]}"}
        )

    needs_rewrite = any(item["severity"] == "high" for item in debt_items)
    score = max(
        100
        - len(debt_items) * 10
        - len(ngram_repeats) * AI_NGRAM_SCORE_PER_HIT
        - (5 if needs_rewrite else 0),
        40,
    )
    return {
        "issues": issues,
        "debt_items": debt_items,
        "needs_rewrite": needs_rewrite,
        "score": score,
        "opening": cur_opening,
        "previous_opening": prev_opening,
    }


# ══════════════════════════════════════════════════════
# M3 AI 味局部重写（超阈值触发，只重写开头窗口，前文保持）
#
# 对应 PRD M3-③：detect_ai_artifacts 返回 needs_rewrite=True（存在 high 项）时，
# 对章节开头窗口（前 300 字）执行局部重写，针对性消除「开头重复 / 句式单调 /
# 连接词滥用」，正文其余部分保持不动。LLM 失败 / 结果过短 / 无变化均降级返回原文，
# 绝不阻塞整书任务（与 hook_service.rewrite_ending 同一降级约定）。
# ══════════════════════════════════════════════════════

AI_OPENING_REWRITE_LENGTH = 300       # 开头重写窗口（前 300 字）
MIN_REWRITTEN_OPENING_LENGTH = 60     # 重写开头长度下限（过短视为无效，降级原文）

REWRITE_OPENING_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _extract_opening(content: str, length: int = AI_OPENING_REWRITE_LENGTH) -> str:
    """截取开头窗口（前 length 字）。"""
    stripped = (content or "").strip()
    if not stripped:
        return ""
    if len(stripped) <= length:
        return stripped
    return stripped[:length]


def _replace_opening(content: str, new_opening: str) -> str:
    """保留前文，替换开头 AI_OPENING_REWRITE_LENGTH 字。"""
    stripped = (content or "").strip()
    new_opening = (new_opening or "").strip()
    if not stripped:
        return new_opening
    if not new_opening:
        return stripped
    opening = _extract_opening(stripped, AI_OPENING_REWRITE_LENGTH)
    tail = stripped[len(opening):] if opening else stripped
    return (new_opening + "\n\n" + tail.lstrip()).strip()


def _build_rewrite_opening_prompt(content: str, ai: dict) -> str:
    """构造局部重写 prompt：带上 detect_ai_artifacts 的问题清单，让 LLM 针对性修复。"""
    issues = ai.get("issues") or []
    ordered = [item for item in issues if item.get("severity") == "high"] + [
        item for item in issues if item.get("severity") != "high"
    ]
    problem_lines = []
    for item in ordered[:5]:
        problem_lines.append(
            f"- {item.get('type', '问题')}：{item.get('reason', '')}"
            f"（建议：{item.get('suggestion', '')}）"
        )
    problems = "\n".join(problem_lines) if problem_lines else "（检测到明显 AI 味）"
    opening = _extract_opening(content, AI_OPENING_REWRITE_LENGTH)
    return (
        "以下是本章开头（前 300 字），检测到明显的 AI 味问题：\n"
        f"问题清单：\n{problems}\n\n"
        f"当前开头：\n{opening}\n\n"
        "请重写这段开头，针对性消除上述问题（开头避免与上一章雷同、"
        "打散相同句式、删减高频连接词），保持角色、场景、情节与原文一致，"
        "文风与原作一致。只输出重写后的开头正文。"
    )


async def rewrite_opening(content: str, ai: dict) -> str:
    """局部重写章节开头（前 300 字）消除 AI 味；前文保持不动。

    LLM 失败 / 结果过短 / 与原文无差异时降级返回原内容（不阻塞整书任务）。
    """
    if not content or not content.strip():
        return content
    token = set_llm_audit_context(role="style")
    try:
        reply = await get_runtime_provider(llm).chat(
            [
                {"role": "system", "content": REWRITE_OPENING_SYSTEM_PROMPT},
                {"role": "user", "content": _build_rewrite_opening_prompt(content, ai)},
            ],
            max_tokens=600,
        )
        new_opening = repair_text(reply).strip()
        if new_opening and len(new_opening) >= MIN_REWRITTEN_OPENING_LENGTH:
            return _replace_opening(content, new_opening)
    except (LLMConfigurationError, LLMResponseError):
        pass
    finally:
        reset_llm_audit_context(token)
    return content


# ══════════════════════════════════════════════════════
# C3：同章原句重复的自动重写闭环（按片段位置局部重写，非前 300 字）
#
# Codex 盘点 C3：检测命中 high（≥20 字完整句/段复制）时，不能只重写前 300 字
# 的 rewrite_opening（重复可能出现在章中任意位置），必须按重复片段的具体
# 位置做局部重写，再复检不降分才落库。
# ══════════════════════════════════════════════════════

NGRAM_REWRITE_WINDOW = 400        # 重复片段前后各取 N 字作为重写窗口
MIN_REWRITTEN_NGRAM_LENGTH = 30   # 重写结果长度下限（过短视为无效）
MAX_NGRAM_SENTENCE_LEN = 240      # 句子边界解析上限：无标点超长段退回片段替换

# 中文/英文句末标点 + 换行 = 句子边界
_SENTENCE_ENDERS = set("。！？!?；;\n")
# 句末标点后的闭合引号/括号（Codex P2-2：应吸收进句子，避免模型输出完整引号产生双引号）
_SENTENCE_CLOSERS = set("”’」』】》〉")
_ELLIPSIS = "……"

REWRITE_NGRAM_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

# 超长无标点段退回片段替换时使用的提示（Codex P2-2：只要求模型输出片段本身，
# 避免"整句输出塞进片段位置"的契约错位）
REWRITE_NGRAM_FRAGMENT_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _ngram_window_positions(text: str, phrase: str) -> list[int]:
    """找到重复片段在正文（去空白前）中的出现位置。"""
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(phrase, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _extract_ngram_window(text: str, pos: int, phrase_len: int) -> tuple[int, int, str]:
    """以重复片段为中心，取前后各 NGRAM_REWRITE_WINDOW 字的**上下文窗口**（仅供 LLM 参考）。

    返回 (start, end, window)。注意：此窗口只用于给 LLM 提供上下文理解，
    **不用于替换**——替换针对重复片段所在的完整句子（见 _ngram_sentence_bounds）。
    """
    start = max(0, pos - NGRAM_REWRITE_WINDOW)
    end = min(len(text), pos + phrase_len + NGRAM_REWRITE_WINDOW)
    return start, end, text[start:end]


def _ngram_sentence_bounds(text: str, pos: int, phrase_len: int) -> tuple[int, int]:
    """解析重复片段所在**完整句子**的边界 (left, right)（Codex P1/P2-1/P2-2）。

    - 左边界：向片段左侧扩展，直到句末标点/换行之后（跳过句子开头空白）；
    - 右边界：向片段右侧扩展，直到句末标点（含标点），并**吸收句末标点后的
      闭合引号/括号**（`”’』」】》〉`）与**中文省略号**（`……`），
      避免模型输出完整引号/省略号时产生双引号（Codex P2-2）；
    - 若扩展出的句子过长（无标点的超长段），退回片段本身范围，避免吞并大段正文。
    """
    left = pos
    while left > 0:
        ch = text[left - 1]
        if ch in _SENTENCE_ENDERS:
            break
        left -= 1
    right = pos + phrase_len
    while right < len(text):
        if text.startswith(_ELLIPSIS, right):
            right += 2  # 吸收中文省略号
            break
        ch = text[right]
        if ch in _SENTENCE_ENDERS:
            right += 1  # 包含句末标点
            # 吸收句末标点后的闭合引号/括号
            while right < len(text) and text[right] in _SENTENCE_CLOSERS:
                right += 1
            break
        right += 1
    if right - left > MAX_NGRAM_SENTENCE_LEN:
        return pos, pos + phrase_len
    return left, right


def _replace_ngram_sentence(text: str, left: int, right: int, new_sentence: str) -> str:
    """用改写后的完整句子替换 [left, right) 区间（区间外正文完整保留）。"""
    new_piece = (new_sentence or "").strip()
    if not new_piece:
        return text
    return text[:left] + new_piece + text[right:]


def _replace_ngram_occurrence(text: str, phrase: str, new_text: str) -> str:
    """用改写文本替换重复片段的**首次出现**（备用路径：无标点超长段时使用）。

    - 找到第一个出现位置；
    - 仅替换该片段（保留片段前后的标点/空白，保证正文结构完整）；
    - 其他位置（正文前后）完全保留。
    返回替换后的完整正文；若未找到片段返回原文。
    """
    pos = text.find(phrase)
    if pos < 0:
        return text
    new_piece = (new_text or "").strip()
    if not new_piece:
        return text
    return text[:pos] + new_piece + text[pos + len(phrase):]


async def rewrite_ngram_repeat(content: str, ai: dict) -> str:
    """按同章 n-gram 重复片段的**实际位置**做局部重写（Codex C3/D1/P2-1 闭环）。

    - 只处理 issues 中 type=同章原句重复 且 severity=high 的项（≥20 字完整复制）；
    - 窗口（前后 400 字）仅作为 LLM 的上下文参考；
    - **替换针对重复片段所在的完整句子**：`_ngram_sentence_bounds` 解析句子边界，
      提示模型改写整句，`_replace_ngram_sentence` 替换整句（Codex P2-1：避免"片段替换
      + 句尾残留"产生双标点/重复尾句）；无标点超长段退回片段替换；
    - 改写后再次运行 detect_ai_artifacts 复检（调用方负责），本函数仅返回改写结果；
    - LLM 失败 / 过短 / 无变化均降级返回原文（不阻塞整书任务）。
    """
    if not content or not content.strip():
        return content
    repeats = [
        item for item in (ai.get("issues") or [])
        if item.get("type") == "同章原句重复"
        and item.get("severity") == "high"
        and item.get("original")
    ]
    if not repeats:
        return content

    result = content
    token = set_llm_audit_context(role="style")
    try:
        for repeat in repeats[:2]:  # 最多修 2 处（避免多次 LLM 调用拖慢任务）
            phrase = repeat.get("original") or ""
            if not phrase or phrase not in result:
                continue
            positions = _ngram_window_positions(result, phrase)
            if not positions:
                continue
            pos = positions[0]
            left, right = _ngram_sentence_bounds(result, pos, len(phrase))
            sentence = result[left:right]
            # 上下文窗口仅供 LLM 理解，不用于替换
            _, _, window = _extract_ngram_window(result, pos, len(phrase))
            # 超长无标点段退回片段替换 → 用"只输出片段"提示（Codex P2-2：避免契约错位）
            is_fragment = (left, right) == (pos, pos + len(phrase))
            system_prompt = REWRITE_NGRAM_FRAGMENT_SYSTEM_PROMPT if is_fragment else REWRITE_NGRAM_SYSTEM_PROMPT
            user_content = (
                f"重复片段：{phrase}\n\n所在段落（只改写片段本身，其余保持）：\n{window}"
                if is_fragment else
                f"重复句子：{sentence}\n\n所在段落（只改写上面这句，其余保持）：\n{window}"
            )
            try:
                reply = await get_runtime_provider(llm).chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=600,
                )
                new_text = repair_text(reply).strip()
                if len(new_text) < MIN_REWRITTEN_NGRAM_LENGTH:
                    continue
                # 替换重复片段所在完整句子（整句路径）；超长段则替换片段本身
                new_content = (
                    _replace_ngram_sentence(result, left, right, new_text)
                    if not is_fragment
                    else _replace_ngram_occurrence(result, phrase, new_text)
                )
                if new_content == result:
                    continue
                # 复检：重写后该片段不应再重复出现（整句/片段被替换）
                if new_content.count(phrase) >= result.count(phrase):
                    continue  # 重复未消除，不落库
                result = new_content
            except (LLMConfigurationError, LLMResponseError):
                pass
    finally:
        reset_llm_audit_context(token)
    return result


def _should_accept_ngram_rewrite(original_ai: dict, recheck_ai: dict) -> bool:
    """判定是否接受 n-gram 重写结果（Codex D2：复检不通过必须回滚）。

    仅在同时满足以下条件时才接受：
      1. 复检无高严重度同章原句重复（重写真正消除了问题）；
      2. 复检综合评分 >= 原始评分（不降分）。
    否则返回 False，调用方必须恢复原文且不增加 rewrite_count。
    """
    recheck_ngram = [
        i for i in (recheck_ai.get("issues") or [])
        if i.get("type") == "同章原句重复" and i.get("severity") == "high"
    ]
    if recheck_ngram:
        return False
    original_score = original_ai.get("score") or 0
    recheck_score = recheck_ai.get("score") or 0
    return recheck_score >= original_score
