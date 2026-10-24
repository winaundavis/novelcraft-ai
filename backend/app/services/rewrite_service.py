"""文笔局部重写服务（文笔质量固化 · Codex 方案执行顺序第 8 步）。

背景：
- 生成侧已通过 prompt_contract 把评分量表 D1-D10 检查点固化进提示词（尽量
  「生成时就不犯错」）；本服务负责「生成后仍不达标」的局部重写兜底。
- 针对 5 种局部场景（开头/对话/行动/情绪/结尾）分别重写，不整章重写；
- 每次重写都落审计痕迹（chapter_rewrites 表）：原文/重写原因/重写结果/评分前后；
- 自动回退：重写后事实一致性（fact_check）或综合评分下降 → 回退原文；
- 评分量表标注「必须人工」的维度（D2/D4/D7/D8 等）只记质量债（debt），不自动重写。

设计原则（对齐既有服务约定，如 hook_service / deai_service / review_service）：
- 所有 LLM 调用失败均降级（返回原文 / 跳过重写），绝不阻塞生成任务；
- 重写服务本身不改 chapters.content（只写审计表），提供 apply_rewrite 应用入口；
- 触发阈值：评分量表 6.2 —— 任一维度 ≤2 分触发对应局部重写；
- 与既有能力打通：结尾重写复用 hook_service.rewrite_ending（D9）；
  情绪/去 AI 味重写复用 deai_service.deai_rewrite（D10/D6）；
  开头重写复用 review_service.rewrite_opening（D1）。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from app.dao import chapter_rewrites as chapter_rewrites_dao
from app.dao import chapters as chapters_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import projects as projects_dao
from app.db import get_conn, utc_today
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.roles import ROLE_WRITING
from app.services import deai_service
from app.services import fact_check_service
from app.services import hook_service
from app.services import memory_service
from app.services import review_service
from app.utils.text_repair import repair_text
from app.utils.word_count import count_words

# ── 重写类型（5 种） ──────────────────────────────────
REWRITE_TYPE_OPENING = "opening"     # 开头
REWRITE_TYPE_DIALOGUE = "dialogue"   # 对话
REWRITE_TYPE_ACTION = "action"       # 行动
REWRITE_TYPE_EMOTION = "emotion"     # 情绪
REWRITE_TYPE_ENDING = "ending"       # 结尾
VALID_REWRITE_TYPES = {
    REWRITE_TYPE_OPENING,
    REWRITE_TYPE_DIALOGUE,
    REWRITE_TYPE_ACTION,
    REWRITE_TYPE_EMOTION,
    REWRITE_TYPE_ENDING,
}
REWRITE_TYPE_LABELS = {
    REWRITE_TYPE_OPENING: "开头",
    REWRITE_TYPE_DIALOGUE: "对话",
    REWRITE_TYPE_ACTION: "行动",
    REWRITE_TYPE_EMOTION: "情绪",
    REWRITE_TYPE_ENDING: "结尾",
}

# ── 阈值 / 窗口 ───────────────────────────────────────
REWRITE_TRIGGER_SCORE = 2            # 评分量表 6.2：任一维度 ≤2 触发局部重写
MIN_REWRITE_CONTENT_LENGTH = 200     # 触发重写的最小正文长度（过短不触发，避免短文本误重写）
OPENING_WINDOW = 300                 # 开头重写窗口（复用 review_service）
DIALOGUE_WINDOW = 1200               # 对话重写窗口
ACTION_WINDOW = 1200                 # 行动重写窗口
ENDING_WINDOW = 400                  # 结尾重写窗口（复用 hook_service.HOOK_ENDING_LENGTH）
MIN_SECTION_REWRITTEN_LENGTH = 40    # 局部重写结果长度下限（过短视为无效 → 降级原文）

# ── 评分量表维度配置（style-rubric-v1.md 第二/八节） ──
# auto=True 的维度有可靠的自动化评分信号，≤2 分时自动触发对应局部重写；
# manual=True 的维度（评分量表标注「必须人工」：D2/D4/D7/D8 的语义判断）只记质量债，
# 系统不自动重写（避免基于不可靠的自动评分去动文本）。
RUBRIC_DIMENSIONS: dict[str, dict[str, Any]] = {
    "D1": {"label": "开篇建立", "rewrite_type": REWRITE_TYPE_OPENING, "auto": True, "manual": False},
    "D2": {"label": "章节有效变化", "rewrite_type": None, "auto": False, "manual": True},
    "D3": {"label": "对话推进", "rewrite_type": REWRITE_TYPE_DIALOGUE, "auto": True, "manual": False},
    "D4": {"label": "角色辨识度", "rewrite_type": None, "auto": False, "manual": True},
    "D5": {"label": "冲突升级", "rewrite_type": REWRITE_TYPE_ACTION, "auto": True, "manual": False},
    "D6": {"label": "情绪三阶段", "rewrite_type": REWRITE_TYPE_EMOTION, "auto": True, "manual": False},
    "D7": {"label": "场景具体化", "rewrite_type": None, "auto": False, "manual": True},
    "D8": {"label": "节奏变化", "rewrite_type": None, "auto": False, "manual": True},
    "D9": {"label": "章节钩子", "rewrite_type": REWRITE_TYPE_ENDING, "auto": True, "manual": False},
    "D10": {"label": "AI 痕迹", "rewrite_type": REWRITE_TYPE_EMOTION, "auto": True, "manual": False},
}

# 评分量表权重（style-rubric-v1.md 第二节，合计 100%）
RUBRIC_WEIGHTS: dict[str, float] = {
    "D1": 0.12, "D2": 0.08, "D3": 0.10, "D4": 0.12, "D5": 0.12,
    "D6": 0.10, "D7": 0.08, "D8": 0.08, "D9": 0.12, "D10": 0.08,
}

# ── 启发式评分用词表（零 LLM 成本） ────────────────────
_ENV_OPENING_MARKERS = ("天空", "天气", "阳光", "清晨", "夜晚", "月光", "街", "风", "雨", "晨光", "夕阳", "夜")
_CONFLICT_MARKERS = ("冲突", "危险", "杀", "追", "逼", "怒", "惊", "决", "背叛", "威胁", "堵", "逃", "斗", "战")
_SUMMARY_STYLE_MARKERS = ("经过一番", "终于打败", "达成了一致", "经过一场", "激战之后", "经过一番激战", "一番交谈")
_EMPTY_EMOTION_MARKERS = ("五味杂陈", "百感交集", "涌上心头", "难以言喻", "无比温暖", "复杂的神色", "莫名地", "一股暖流")
_VAQUE_ADJ_MARKERS = ("巨大", "强大", "深深", "无比", "极其", "十分", "非常", "无尽", "无边", "深邃", "沉重", "冰冷", "压抑", "幽暗")
# 对话标签重复检测（评分量表 D3 检查项）
_DIALOGUE_TAG_MARKERS = ("说道", "道：", "说：", "问道", "回答", "开口")
_DIALOGUE_QUOTES = ("“", "”", "「", "」", "：")


def _strip(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _count_words(text: str) -> int:
    # 与前端 `content.replace(/\s/g, '').length` 同口径（见 app.utils.word_count）。
    return count_words(text)


# ══════════════════════════════════════════════════════
# 启发式评分量表（D1-D10 → 1-5 分 + 加权总分）
#
# 定位：仅供「自动触发局部重写」与「自动回退对比」使用；评分量表第八节明确
# 「能确定性的交给自动化，能语义判断的交给 LLM 评审档 + 网文大师人工」，
# 最终裁决权在人工。manual 维度的低分只记质量债，不自动重写。
# ══════════════════════════════════════════════════════


def _score_d1_opening(content: str, previous_text: str = "") -> int:
    """D1 开篇建立：前 500 字是否有具体行动 + 未解决冲突 + 非环境开场。"""
    opening = _strip(content)[:500]
    if not opening:
        return 1
    score = 3
    # 非环境/背景式开场
    if not any(opening.startswith(m) for m in _ENV_OPENING_MARKERS):
        score += 1
    # 有冲突/悬念词
    if any(m in opening for m in _CONFLICT_MARKERS):
        score += 1
    # 与上一章开头重复（AI 味）→ 降
    if previous_text:
        prev = _strip(previous_text)[:30]
        cur = opening[:30]
        if prev and cur and _char_overlap(cur, prev) >= 0.7:
            score -= 2
    return max(1, min(5, score))


def _char_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    return len(set_a & set_b) / max(len(set_a), 1)


def _score_d3_dialogue(content: str) -> int:
    """D3 对话推进：对话占比 + 对话标签重复 + 是否有对话。"""
    stripped = _strip(content)
    if not stripped:
        return 1
    has_dialogue = any(m in content for m in _DIALOGUE_QUOTES)
    if not has_dialogue:
        return 2  # 无对话 → 对话推进差（量表 D3：对话可整体删除=1；无对话场景给 2）
    ratio = sum(content.count(q) for q in ("“", "「")) / max(len(stripped), 1)
    score = 4
    if ratio < 0.02 or ratio > 0.6:
        score = 3  # 对话占比异常（过低/过高）
    # 对话标签重复（说道/道：…）→ 降
    tag_count = sum(content.count(m) for m in _DIALOGUE_TAG_MARKERS)
    if tag_count >= 8:
        score -= 1
    return max(1, min(5, score))


def _score_d5_conflict(content: str) -> int:
    """D5 冲突升级：是否出现「总结腔」式一次性化解冲突。"""
    if any(m in content for m in _SUMMARY_STYLE_MARKERS):
        return 2  # 总结腔化解冲突（量表 4.2）→ 低分
    if any(m in content for m in _CONFLICT_MARKERS):
        return 4
    return 3


def _score_d6_emotion(content: str) -> int:
    """D6 情绪三阶段：空泛心理标签出现 → 低分。"""
    hits = sum(content.count(m) for m in _EMPTY_EMOTION_MARKERS)
    if hits >= 3:
        return 1
    if hits >= 1:
        return 2
    return 3


def _score_d7_scene(content: str) -> int:
    """D7 场景具体化：万能形容词密度。"""
    stripped = _strip(content)
    if not stripped:
        return 1
    density = sum(stripped.count(m) for m in _VAQUE_ADJ_MARKERS) / max(len(stripped) / 500.0, 1.0)
    if density >= 6:
        return 2  # 每 500 字 ≥6 个万能形容词 → 空泛
    if density >= 3:
        return 3
    return 4


def _score_d8_rhythm(content: str) -> int:
    """D8 节奏变化：段落长度方差过小（机械化均匀）→ 低分。"""
    paragraphs = [p for p in (content or "").split("\n\n") if p.strip()]
    if len(paragraphs) < 3:
        return 3
    lengths = [len(_strip(p)) for p in paragraphs]
    mean = sum(lengths) / len(lengths)
    variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
    std = variance ** 0.5
    if std < mean * 0.15:
        return 2  # 段落长度高度均匀 → 机械化（量表 D8）
    if std < mean * 0.3:
        return 3
    return 4


def _score_d10_ai(content: str, ai: dict[str, Any] | None = None) -> int:
    """D10 AI 痕迹：复用 detect_ai_artifacts 的确定性得分（0-100 → 1-5）。"""
    ai = ai or review_service.detect_ai_artifacts(content)
    score = ai.get("score", 100)
    has_high = any(item.get("severity") == "high" for item in (ai.get("debt_items") or []))
    if has_high or score < 70:
        return 1  # 量表 6.2：D10 ≤2 → 强制去 AI 味重写
    if score < 90:
        return 3
    return 5


def heuristic_rubric_scores(
    content: str,
    previous_text: str = "",
    ai: dict[str, Any] | None = None,
    has_hook: bool | None = None,
) -> dict[str, Any]:
    """启发式评分量表（D1-D10 → 1-5 + 加权总分，10 分制）。

    返回：
      {"dimensions": {"D1": .., ..., "D10": ..},
       "total": 加权总分（10 分制，公式见 style-rubric-v1.md 6.1）,
       "method": "heuristic"}
    """
    dimensions = {
        "D1": _score_d1_opening(content, previous_text),
        "D2": 3,  # 章节有效变化 = 「必须人工」语义判断，默认中性，只记债务不触发
        "D3": _score_d3_dialogue(content),
        "D4": 3,  # 角色辨识度 = 「必须人工」，默认中性
        "D5": _score_d5_conflict(content),
        "D6": _score_d6_emotion(content),
        "D7": _score_d7_scene(content),
        "D8": _score_d8_rhythm(content),
        "D9": (5 if has_hook else (1 if has_hook is False else 3)),
        "D10": _score_d10_ai(content, ai),
    }
    weighted = sum(
        dimensions[dim] * RUBRIC_WEIGHTS.get(dim, 0.0) for dim in dimensions
    )
    total = weighted / 5.0 * 10.0
    return {
        "dimensions": dimensions,
        "total": round(total, 2),
        "method": "heuristic",
    }


# ══════════════════════════════════════════════════════
# 局部重写：窗口提取 / 替换 / 5 种类型
# ══════════════════════════════════════════════════════

DIALOGUE_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

ACTION_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _replace_window(content: str, window: str, rewritten: str) -> str:
    """把正文开头的 window 窗口替换为 rewritten（保留尾部）。"""
    stripped = (content or "").strip()
    rewritten = (rewritten or "").strip()
    if not stripped:
        return rewritten
    if not rewritten:
        return stripped
    if window and stripped.startswith(window):
        tail = stripped[len(window):]
        if tail.strip():
            return (rewritten + "\n\n" + tail.lstrip()).strip()
        return rewritten
    return stripped


async def _rewrite_section_llm(content: str, rewrite_type: str, allow_llm_call: Callable[[str, int], bool] | None = None) -> str:
    """对话/行动类型的通用 LLM 局部重写（窗口内重写，尾部保留）。"""
    window_len = DIALOGUE_WINDOW if rewrite_type == REWRITE_TYPE_DIALOGUE else ACTION_WINDOW
    system_prompt = DIALOGUE_SYSTEM_PROMPT if rewrite_type == REWRITE_TYPE_DIALOGUE else ACTION_SYSTEM_PROMPT
    window = (content or "").strip()[:window_len]
    if len(window) < MIN_SECTION_REWRITTEN_LENGTH:
        return content
    if allow_llm_call and not allow_llm_call("style_rewrite", 1600):
        return content
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"以下是章节局部内容（前 {window_len} 字窗口）：\n{window}\n\n"
                        "请按上述要求重写这段内容，保持人物、场景、情节与核心事实不变。"
                        "只输出重写后的正文，不要输出解释。"
                    ),
                },
            ],
            role=ROLE_WRITING,
            max_tokens=1600,
        )
        rewritten = repair_text(reply).strip()
        if rewritten and len(_strip(rewritten)) >= MIN_SECTION_REWRITTEN_LENGTH:
            return _replace_window(content, window, rewritten)
    except (LLMConfigurationError, LLMResponseError):
        pass
    return content


async def _do_rewrite(project_id: str, content: str, rewrite_type: str, allow_llm_call: Callable[[str, int], bool] | None = None) -> str:
    """按重写类型执行局部重写（LLM 失败一律降级返回原文，绝不阻塞）。"""
    if rewrite_type == REWRITE_TYPE_OPENING:
        if allow_llm_call and not allow_llm_call("style_rewrite", 1200):
            return content
        ai = review_service.detect_ai_artifacts(content)
        return await review_service.rewrite_opening(content, ai)
    if rewrite_type == REWRITE_TYPE_ENDING:
        return await hook_service.rewrite_ending(content, allow_llm_call)
    if rewrite_type == REWRITE_TYPE_EMOTION:
        if allow_llm_call and not allow_llm_call("style_rewrite", 1800):
            return content
        # 情绪/去 AI 味重写复用 deai_service.deai_rewrite（去 AI 味改写作为
        # 「局部重写-情绪/对话」的一种实现；其 DEAI_SYSTEM_PROMPT 第 4 条
        # 专门处理对话口语化，第 6 条处理情绪直白标签）。
        result = await deai_service.deai_rewrite(project_id, content)
        return result.get("result") or content
    if rewrite_type in (REWRITE_TYPE_DIALOGUE, REWRITE_TYPE_ACTION):
        return await _rewrite_section_llm(content, rewrite_type, allow_llm_call)
    return content


# ══════════════════════════════════════════════════════
# 自动回退守卫 + 单次重写（审计 + 回退）
# ══════════════════════════════════════════════════════


async def _rewrite_with_guard(
    project_id: str,
    chapter_id: str,
    content: str,
    rewrite_type: str,
    reason: str,
    memory_context: dict[str, Any] | None = None,
    previous_text: str = "",
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """对指定类型执行一次局部重写，带审计痕迹 + 自动回退。

    流程：
      1. 记评分前（启发式加权总分）；
      2. 执行局部重写（LLM 失败降级返回原文 → 无变化则跳过）；
      3. 校验：
         a. 事实一致性：LLM 核查（memory_context 提供时；失败降级视为通过）；
         b. 综合评分：重写后 < 重写前 → 回退；
         c. 结尾专用：重写后仍无强钩 → 回退；
      4. 落审计表（chapter_rewrites）：original/rewritten/reason/score_before/
         score_after/status（applied 或 reverted）。

    返回 (最终正文, 审计记录 dict 或 None)。审计记录含 rewrite_id 与 status。
    """
    stripped_original = _strip(content)
    if not content or len(stripped_original) < MIN_REWRITE_CONTENT_LENGTH:
        return content, None

    scores_before = heuristic_rubric_scores(content, previous_text=previous_text)
    score_before = scores_before["total"]

    # Opening/emotion delegates already own their provider call. Section/ending
    # receive the shared callback and reserve before their direct request.
    rewritten = await _do_rewrite(project_id, content, rewrite_type, allow_llm_call)
    if not rewritten or _strip(rewritten) == stripped_original:
        return content, None  # 重写失败 / 无变化 → 不落审计

    # ── 校验（自动回退） ──
    keep = True
    revert_reason = ""
    # 3a. 事实一致性：重写不得引入事实矛盾（LLM 失败降级视为通过）
    if memory_context is not None:
        try:
            fc = await fact_check_service.check_chapter(
                project_id, chapter_id, rewritten, memory_context, allow_llm_call
            )
            if fc.get("budget_exhausted"):
                keep = False
                revert_reason = "chapter QA budget exhausted during rewrite validation"
            if fc.get("result") == "FAIL":
                keep = False
                revert_reason = "重写后事实一致性核查 FAIL（引入事实矛盾）"
        except Exception:  # noqa: BLE001  核查失败降级视为通过，不阻塞
            pass
    # 3b. 综合评分：重写后不得下降
    if keep:
        scores_after = heuristic_rubric_scores(rewritten, previous_text=previous_text)
        if scores_after["total"] < score_before:
            keep = False
            revert_reason = (
                f"综合评分下降（{score_before} → {scores_after['total']}）"
            )
    # 3c. 结尾专用：仍无强钩 → 回退
    if keep and rewrite_type == REWRITE_TYPE_ENDING:
        try:
            hook = await hook_service.check_hook(rewritten, allow_llm_call)
            if hook.get("budget_exhausted"):
                keep = False
                revert_reason = "chapter QA budget exhausted during hook validation"
            if not hook.get("has_hook"):
                keep = False
                revert_reason = "结尾重写后仍未形成强钩子"
        except Exception:  # noqa: BLE001  判定失败视为通过，不阻塞
            pass

    scores_after = heuristic_rubric_scores(rewritten, previous_text=previous_text)
    status = (
        chapter_rewrites_dao.REWRITE_STATUS_APPLIED
        if keep
        else chapter_rewrites_dao.REWRITE_STATUS_REVERTED
    )
    reason_full = reason
    if revert_reason:
        reason_full = f"{reason}；自动回退：{revert_reason}"

    rewrite_id = chapter_rewrites_dao.insert_rewrite(
        project_id,
        chapter_id,
        rewrite_type,
        original=content,
        rewritten=rewritten,
        reason=reason_full,
        score_before=score_before,
        score_after=scores_after["total"],
        status=status,
    )
    return (rewritten if keep else content), {
        "rewrite_id": rewrite_id,
        "rewrite_type": rewrite_type,
        "status": status,
        "reason": reason_full,
        "score_before": score_before,
        "score_after": scores_after["total"],
        "reverted": not keep,
    }


# ══════════════════════════════════════════════════════
# 编排：评分量表驱动的局部重写链路
# ══════════════════════════════════════════════════════


async def run_style_rewrite_chain(
    project_id: str,
    chapter_id: str,
    content: str,
    previous_text: str = "",
    memory_context: dict[str, Any] | None = None,
    is_first_chapter: bool = False,
    has_hook: bool | None = None,
    max_rewrites: int = 1,
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> dict[str, Any]:
    """评分量表驱动的文笔局部重写编排（自检闭环第 ④ 步）。

    流程：
      1. 启发式评分（D1-D10，1-5 分）；
      2. 任一维度 ≤2 → 按 RUBRIC_DIMENSIONS 配置触发：
         - auto 维度：执行对应局部重写（含审计 + 自动回退）；
         - manual 维度（「必须人工」）：只记质量债（debt），不自动重写；
         - 第一章开篇（D1 ≤2）：量表 6.2 要求整章重写（人工介入），只记债务；
      3. 一次重写生效后对当前正文重新评分（后续维度基于最新正文判断）。

    Codex P1-2（调用成本）：`max_rewrites` 限制**实际执行的 LLM 重写次数**（默认 1）。
    只对**最低分**的 auto 维度执行一次定向重写，其余低分维度一律记质量债（debt），
    不逐维度触发 LLM——把每章文笔重写从「最多 10 次 LLM」压到「最多 1 次」。

    返回：
      {
        "content": 最终正文（重写后可能已应用，未回退时保留原文）,
        "rewrite_count": 成功应用的重写次数,
        "debt_count": 质量债条数（manual 维度低分 + 第一章整章重写标记）,
        "debt_items": 质量债明细,
        "rewrites": 本次产生的重写审计明细,
      }
    全程降级：任何 LLM 失败返回原内容，绝不抛出异常阻塞整书任务。
    """
    result: dict[str, Any] = {
        "content": content,
        "rewrite_count": 0,
        "debt_count": 0,
        "debt_items": [],
        "rewrites": [],
    }
    if not content or len(_strip(content)) < MIN_REWRITE_CONTENT_LENGTH:
        return result

    current = content
    scores = heuristic_rubric_scores(
        current, previous_text=previous_text, has_hook=has_hook
    )

    # Codex P1-2：先收集所有低分维度，按分数升序（最低分优先）排序，
    # 只对前 max_rewrites 个 auto 维度执行 LLM 重写，其余记质量债。
    low_dims = [
        (dim, info, scores["dimensions"].get(dim, 3))
        for dim, info in RUBRIC_DIMENSIONS.items()
        if scores["dimensions"].get(dim, 3) <= REWRITE_TRIGGER_SCORE
    ]
    low_dims.sort(key=lambda x: x[2])  # 分数升序，最低分优先

    auto_budget = max(0, int(max_rewrites))
    for dim, info, score in low_dims:
        # 「必须人工」维度（或第一章开篇需整章重写）：只记质量债，不自动重写
        manual_reason = ""
        if info.get("manual"):
            manual_reason = (
                f"{info['label']}（D{dim[1:]}）评分 {score}/5 ≤ 2，"
                "属「必须人工」维度，系统仅标记质量债，不自动重写。"
            )
        elif dim == "D1" and is_first_chapter:
            manual_reason = (
                "第一章开篇（D1）评分 ≤ 2，量表 6.2 要求整章重写（人工介入），"
                "系统仅标记质量债，不自动局部重写。"
            )
        if manual_reason:
            result["debt_items"].append(
                {
                    "dimension": dim,
                    "label": info["label"],
                    "score": score,
                    "manual": True,
                    "detail": manual_reason,
                }
            )
            result["debt_count"] += 1
            continue

        rewrite_type = info.get("rewrite_type")
        if not rewrite_type:
            continue

        # Codex P1-2：超出单章重写预算的 auto 维度 → 只记质量债，不触发 LLM
        if auto_budget <= 0:
            result["debt_items"].append(
                {
                    "dimension": dim,
                    "label": info["label"],
                    "score": score,
                    "manual": False,
                    "detail": (
                        f"{info['label']}（D{dim[1:]}）评分 {score}/5 ≤ 2，"
                        f"但已超出单章重写预算（max_rewrites={max_rewrites}），"
                        "系统仅标记质量债，不自动重写。"
                    ),
                }
            )
            result["debt_count"] += 1
            continue

        reason = f"{info['label']}（D{dim[1:]}）评分 {score}/5，量表阈值 ≤2 触发局部重写"
        new_content, record = await _rewrite_with_guard(
            project_id,
            chapter_id,
            current,
            rewrite_type,
            reason,
            memory_context=memory_context,
            previous_text=previous_text,
            allow_llm_call=allow_llm_call,
        )
        if record:
            result["rewrites"].append(
                {
                    "rewrite_id": record["rewrite_id"],
                    "rewrite_type": record["rewrite_type"],
                    "dimension": dim,
                    "status": record["status"],
                    "reason": record["reason"],
                    "score_before": record["score_before"],
                    "score_after": record["score_after"],
                }
            )
        if new_content != current:
            current = new_content
            result["rewrite_count"] += 1
            auto_budget -= 1
            # 一次重写生效后重新评分（后续维度基于最新正文判断）
            scores = heuristic_rubric_scores(
                current, previous_text=previous_text, has_hook=has_hook
            )

    result["content"] = current
    return result


# ══════════════════════════════════════════════════════
# 应用重写（人工确认入口）
# ══════════════════════════════════════════════════════


def apply_rewrite(rewrite_id: str) -> dict[str, Any] | None:
    """把审计表里的重写稿显式应用到章节正文（人工确认入口，同步核心逻辑）。

    数据安全：重写服务默认不改 chapters.content（只保存 rewritten 供人工确认）；
    本接口是唯一的显式应用入口。应用后：
      - chapters.content = rewritten（更新字数/时间）；
      - 同步项目总字数（BUG-5：应用后项目总字数必须跟进）；
      - chapter_rewrites.status → applied（记录 applied_at）。
    已被自动回退（reverted）的记录不允许应用。

    BUG-5 补充：记忆（FTS/摘要/事实/实体）的异步同步见 apply_rewrite_sync_memory，
    由 API 层在调用本函数后 await 执行（保持本函数同步，兼容既有调用方）。

    返回更新后的重写记录；记录不存在返回 None。
    """
    record = chapter_rewrites_dao.get_rewrite(rewrite_id)
    if not record:
        return None
    if record["status"] == chapter_rewrites_dao.REWRITE_STATUS_REVERTED:
        raise ValueError("该重写已被自动回退（事实一致性或综合评分下降），不可应用。")
    project_id = record["project_id"]
    chapter_id = record["chapter_id"]
    if not chapter_id:
        raise ValueError("该重写记录未关联章节，无法应用。")
    rewritten = record["rewritten"] or ""
    if not rewritten.strip():
        raise ValueError("该重写记录缺少重写结果，无法应用。")

    today = utc_today()
    word_count = _count_words(rewritten)
    with get_conn() as conn:
        # 只更新正文/字数/时间，不改变章节状态机（保持 completed/draft 原状）
        conn.execute(
            "UPDATE chapters SET content = ?, word_count = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (rewritten, word_count, today, chapter_id, project_id),
        )
        # BUG-5：同步项目总字数
        try:
            projects_dao._update_project_word_count_on_conn(conn, project_id, today)
        except Exception:  # noqa: BLE001  项目字数同步失败降级，不阻塞应用
            pass
        conn.commit()
    chapter_rewrites_dao.update_status(
        rewrite_id,
        chapter_rewrites_dao.REWRITE_STATUS_APPLIED,
        applied_at=chapter_rewrites_dao._utc_iso(),
    )
    return chapter_rewrites_dao.get_rewrite(rewrite_id)


async def apply_rewrite_sync_memory(rewrite_id: str) -> None:
    """应用重写后同步长期记忆（BUG-5：FTS/摘要/事实/实体必须跟进新版本）。

    由 API 层在 apply_rewrite 后 await 调用。全部降级：任何失败都不抛异常，
    绝不让记忆同步失败阻断重写应用。
    """
    record = chapter_rewrites_dao.get_rewrite(rewrite_id)
    if not record:
        return
    project_id = record["project_id"]
    chapter_id = record["chapter_id"]
    rewritten = record["rewritten"] or ""
    if not chapter_id or not rewritten.strip():
        return
    try:
        # 刷新 FTS 索引（正文已更新，保证可检索到新内容）
        memory_fts_dao.sync_chapter_fts(project_id, chapter_id)
        # 重算章节摘要（新版本内容）
        new_summary = await memory_service.summarize_chapter(
            rewritten, record.get("chapter_title") or ""
        )
        if new_summary:
            chapters_dao.update_chapter_summary(project_id, chapter_id, new_summary)
            memory_fts_dao.sync_chapter_fts(project_id, chapter_id)  # 摘要变化后再同步一次
        # 重新抽取事实/实体（新版本可能改变设定/状态）——验收 P1-1：
        # replace_chapter_memory 先抽取新版本，成功后把该章旧版本中
        # "新版本不再包含"的事实/实体/关系标记 superseded；抽取失败保留旧版本。
        await memory_service.replace_chapter_memory(project_id, chapter_id, rewritten)
    except Exception:  # noqa: BLE001  记忆同步失败降级，不阻塞应用
        pass
