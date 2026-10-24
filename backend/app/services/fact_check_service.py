"""M3 事实核查自检闭环 —— 生成后强制核查客观矛盾，FAIL 自动重写。

对应 PRD M3-①（参照 show-me-the-story 的「生成→事实核查→重写→冲突分析」闭环）：

流程：
  核查（只查 5 类客观矛盾）→ FAIL 带问题清单重写（≤3 次）
  → 仍失败：冲突根因分析
      ├─ 可调和 → 注入 extra_constraints 再重写一次 + 最终核查
      └─ 不可调和 → 落盘「写作冲突」标记（novel_facts.is_conflict=1），供人工决策

设计原则：
- 判定范围**只查客观矛盾**（角色名/时间线/地点/已发生事件/设定），明确排除主观质量，
  否则会变成无限重写 + token 爆炸（参照项目 README 的警告）。
- 成本控制：核查 prompt 只注入「卷摘要 + 近 5 章摘要 + 事实账本」，不注入全文。
- 所有 LLM 调用失败均降级（跳过核查 / 返回原文 / 不可调和兜底），绝不阻塞整书任务。
- prompt 为自研文案，仅借鉴参照项目的架构思想，不复制其代码/长文本。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.dao import novel_facts as novel_facts_dao
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.roles import ESCALATION_THRESHOLD, ROLE_CHECK, ROLE_REASON
from app.utils.text_repair import repair_text
from app.utils.world_rules import world_rule_lines as world_rule_lines_for_prompt

# ── 常量 ──────────────────────────────────────────────
MAX_FACT_CHECK_RETRIES = 3      # 带问题清单重写上限（PRD）
FACT_CHECK_CONTEXT_CAP = 4000   # 核查上下文（卷摘要+近章摘要+账本+世界规则）总长上限
FACT_CHECK_WORLD_RULE_CHARS = 800   # 世界规则块的字数预算（防止挤掉摘要与账本）
CHECK_CONTENT_CAP = 4000        # 单次核查正文窗口（成本控制，非全文）
REWRITE_CONTENT_CAP = 5000      # 重写时输入的正文窗口
MIN_REWRITE_KEEP_RATIO = 0.4    # 重写结果长度 < 原文 40% 视为无效，丢弃
MIN_REWRITE_LENGTH = 200

# 5 类客观矛盾（PRD 明确范围）
FACT_CHECK_CATEGORIES = ("角色名", "时间线", "地点", "已发生事件", "设定")

FACT_CHECK_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

CONFLICT_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

REWRITE_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


# ── 核查上下文（成本控制：只注入摘要 + 账本，非全文） ──

def build_fact_check_context(memory_context: dict[str, Any] | None) -> str:
    """从分层记忆上下文提取核查所需材料：卷摘要 + 近 5 章摘要 + 事实账本。

    对应 PRD：核查 prompt 只注入「卷摘要 + 近 5 章摘要 + 事实账本」而非全文。
    返回空串表示暂无可用材料（起始章节）。
    """
    mc = memory_context or {}
    lines: list[str] = []
    volume_summaries = mc.get("volume_summaries") or []
    recent_summaries = mc.get("recent_chapter_summaries") or []
    milestones = mc.get("completed_milestones") or ""

    if volume_summaries:
        lines.append("【已完结卷摘要】")
        lines.extend(f"- {summary}" for summary in volume_summaries)
    if recent_summaries:
        lines.append("【最近章节摘要】")
        lines.extend(f"- {summary}" for summary in recent_summaries)
    if milestones:
        lines.append(milestones)  # 事实账本（completed/revealed + 近期 state_changed）

    # 世界规则：核查 prompt 第 5 类要求查“世界观规则矛盾”，但此前**从未把规则给核查员**，
    # 于是这类检查只能靠账本里碰巧记到的内容——等于失效。这里单独给一个预算块。
    world_rule_lines = world_rule_lines_for_prompt(
        mc.get("world_rules") or [], max_chars=FACT_CHECK_WORLD_RULE_CHARS, max_count=20
    )
    if world_rule_lines:
        lines.append("【世界规则（硬约束）】")
        lines.extend(world_rule_lines)

    if not lines:
        return ""
    return "\n".join(lines)[:FACT_CHECK_CONTEXT_CAP]


# ── 核查判定 ──────────────────────────────────────────

def _build_fact_check_prompt(content: str, context_text: str) -> str:
    return (
        "请核查下面的章节与已有故事设定之间是否存在**客观事实矛盾**。"
        f"只查以下 5 类：\n"
        "1. 角色名/称呼：角色姓名、称谓前后不一致（如同一个人被写成两个名字）；\n"
        "2. 时间线：时间先后倒错（如已过去的日期再次出现、事件顺序与摘要矛盾）；\n"
        "3. 地点：地点名称/位置与既有设定不一致或前后矛盾；\n"
        "4. 已发生事件：前文已发生的一次性事件（初次见面、身份揭示、取得信物等）"
        "被当作新事件重复发生；\n"
        "5. 设定：世界观规则、人物能力/状态与既有设定矛盾"
        "（如已死亡的角色再次登场且未作解释）。\n"
        "**重要区分——剧情反转/伏笔揭示不算矛盾**：\n"
        "- 如果章节内容是对既有设定的**有意揭示或反转**（如：前文'下落不明'的角色，"
        "本章明确揭示其被锁在棺材底；前文'身份成谜'的人，本章揭示真实身份），"
        "这是剧情推进，**不算矛盾**，不要报 FAIL；\n"
        "- 只有**无意的前后不一致**（同一事实出现两种互相排斥的说法，且章节未作任何"
        "解释或铺垫，属于作者笔误/设定漂移）才算矛盾。\n"
        "判断标准：章节是否**明确、有意地**给出了新信息来推翻旧设定？"
        "是 → 剧情反转（PASS）；否 → 事实矛盾（FAIL）。\n"
        "明确排除：文笔、节奏、悬念、情绪等主观质量评价，不属于事实矛盾。\n"
        "返回 JSON：{\"result\": \"PASS\" 或 \"FAIL\", "
        "\"confidence\": 0.0-1.0, "
        "\"issues\": [{\"category\": \"角色名|时间线|地点|已发生事件|设定\", "
        "\"detail\": \"矛盾描述（引用章节原文）\", \"fix\": \"建议修正方式\"}]}。\n"
        "confidence 为对判定结果的置信度：0.6 以上视为高置信；低于 0.6 表示材料不足"
        "（如上下文稀缺），会触发更强模型复查。\n"
        "没有矛盾时 issues 返回空数组。\n\n"
        f"已有故事设定（卷摘要 + 近章摘要 + 事实账本）：\n{context_text or '无'}\n\n"
        f"待核查章节正文：\n{content[:CHECK_CONTENT_CAP]}"
    )


def _clamp_confidence(value: Any) -> float:
    """把 LLM 返回的置信度规范到 [0,1]；缺失/非法时默认 1.0（不触发升级，成本优先）。"""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 1.0
    if confidence != confidence:  # NaN 防抖
        return 1.0
    return min(1.0, max(0.0, confidence))


def _parse_fact_check_result(reply: str) -> dict[str, Any]:
    """鲁棒解析核查结果：优先 JSON {result, confidence, issues}，退化字符串匹配。

    第 3 档：解析并保留 confidence（0-1），供置信度升级链判断；缺失时默认 1.0
    （视为高置信，不触发升级，向后兼容且成本优先）。
    """
    if not reply or not reply.strip():
        return {"result": "PASS", "issues": [], "skipped": True, "confidence": 1.0}
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end])
            if isinstance(data, dict):
                result = str(data.get("result") or "").upper()
                issues = data.get("issues") or []
                normalized: list[dict[str, str]] = []
                if isinstance(issues, list):
                    for item in issues:
                        if not isinstance(item, dict):
                            continue
                        normalized.append(
                            {
                                "category": str(item.get("category") or ""),
                                "detail": str(item.get("detail") or ""),
                                "fix": str(item.get("fix") or ""),
                            }
                        )
                return {
                    "result": "PASS" if result == "PASS" else "FAIL",
                    "issues": normalized,
                    "skipped": False,
                    "confidence": _clamp_confidence(data.get("confidence")),
                }
        except (json.JSONDecodeError, ValueError):
            pass

    upper = text.upper()
    if "FAIL" in upper and "PASS" not in upper:
        return {"result": "FAIL", "issues": [], "skipped": False, "confidence": 1.0}
    if "PASS" in upper:
        return {"result": "PASS", "issues": [], "skipped": False, "confidence": 1.0}
    return {"result": "PASS", "issues": [], "skipped": True, "confidence": 1.0}


async def check_chapter(
    project_id: str,
    chapter_id: str,
    content: str,
    memory_context: dict[str, Any] | None = None,
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> dict[str, Any]:
    """LLM 判定章节是否存在客观事实矛盾（含第 3 档置信度升级链）。

    升级链：首次用 check 档（便宜模型）判定 → 若 confidence < 阈值（默认 0.6），
    自动用 reason 档（强模型）复查一次，以强模型结果为准；返回中带 escalated 与
    escalation_count 供上层统计。升级复查失败时降级用原结果，绝不抛异常阻塞整书任务。
    """
    if not content or not content.strip():
        return {
            "result": "PASS", "issues": [], "skipped": True,
            "confidence": 1.0, "escalated": False, "escalation_count": 0,
        }
    context_text = build_fact_check_context(memory_context)
    messages = [
        {"role": "system", "content": FACT_CHECK_SYSTEM_PROMPT},
        {"role": "user", "content": _build_fact_check_prompt(content, context_text)},
    ]
    if allow_llm_call and not allow_llm_call("fact_check", 800):
        return {"result": "PASS", "issues": [], "skipped": True, "budget_exhausted": True, "confidence": 1.0, "escalated": False, "escalation_count": 0}
    try:
        reply = await llm.chat(messages, role=ROLE_CHECK, max_tokens=800)
        result = _parse_fact_check_result(reply)
    except (LLMConfigurationError, LLMResponseError):
        return {
            "result": "PASS", "issues": [], "skipped": True,
            "confidence": 1.0, "escalated": False, "escalation_count": 0,
        }

    # 第 3 档：低置信度 → 用 reason 档（强模型）复查一次，以强模型结果为准
    confidence = float(result.get("confidence") or 1.0)
    if confidence < ESCALATION_THRESHOLD:
        if allow_llm_call and not allow_llm_call("fact_escalation", 800):
            result.update({"budget_exhausted": True, "escalated": False, "escalation_count": 0})
            return result
        try:
            strong_reply = await llm.chat(messages, role=ROLE_REASON, max_tokens=800)
            escalated = _parse_fact_check_result(strong_reply)
            escalated["escalated"] = True
            escalated["escalation_count"] = 1
            return escalated
        except (LLMConfigurationError, LLMResponseError):
            # 升级复查失败 → 用原结果（降级，不抛异常）
            pass
    result["escalated"] = False
    result["escalation_count"] = 0
    return result


# ── 冲突根因分析 ──────────────────────────────────────

def _build_conflict_prompt(
    content: str, issues: list[dict[str, Any]], context_text: str
) -> str:
    issue_lines = "\n".join(
        f"- [{item.get('category', '')}] {item.get('detail', '')}"
        f"（建议：{item.get('fix', '')}）"
        for item in issues
        if item.get("detail")
    ) or "- 无具体问题描述"
    return (
        "以下章节多次重写后仍存在客观事实矛盾。请分析根本原因，判断这些矛盾"
        "是否可以通过补充写作约束调和：\n"
        "返回 JSON：{\"reconcilable\": true/false, \"summary\": \"矛盾概述\", "
        "\"root_cause\": \"根因分析\", "
        "\"extra_constraints\": \"可注入写作 prompt 的补充约束（可调和时给出，否则空字符串）\"}。\n\n"
        f"矛盾清单：\n{issue_lines}\n\n"
        f"已有故事设定（卷摘要 + 近章摘要 + 事实账本）：\n{context_text or '无'}\n\n"
        f"章节正文：\n{content[:3000]}"
    )


def _parse_conflict_analysis(reply: str) -> dict[str, Any]:
    if not reply or not reply.strip():
        return {
            "reconcilable": False,
            "summary": "",
            "root_cause": "LLM 分析返回为空",
            "extra_constraints": "",
        }
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end])
            if isinstance(data, dict):
                return {
                    "reconcilable": bool(data.get("reconcilable")),
                    "summary": str(data.get("summary") or "").strip(),
                    "root_cause": str(data.get("root_cause") or "").strip(),
                    "extra_constraints": str(data.get("extra_constraints") or "").strip(),
                }
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "reconcilable": False,
        "summary": "",
        "root_cause": "冲突分析结果无法解析",
        "extra_constraints": "",
    }


async def analyze_conflict(
    content: str,
    issues: list[dict[str, Any]],
    memory_context: dict[str, Any] | None = None,
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> dict[str, Any]:
    """冲突根因分析。LLM 失败降级为「不可调和」兜底（保守，不误判可调和）。"""
    if allow_llm_call and not allow_llm_call("conflict_analysis", 600):
        return {"reconcilable": False, "summary": "", "root_cause": "budget_exhausted", "extra_constraints": "", "budget_exhausted": True}
    context_text = build_fact_check_context(memory_context)
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": CONFLICT_SYSTEM_PROMPT},
                {"role": "user", "content": _build_conflict_prompt(content, issues, context_text)},
            ],
            role=ROLE_REASON,
            max_tokens=600,
        )
        return _parse_conflict_analysis(reply)
    except (LLMConfigurationError, LLMResponseError):
        return {
            "reconcilable": False,
            "summary": "",
            "root_cause": "LLM 分析失败，无法调和",
            "extra_constraints": "",
        }


# ── 写作冲突落盘 ──────────────────────────────────────

def record_writing_conflict(
    project_id: str,
    chapter_id: str,
    analysis: dict[str, Any] | None,
    issues: list[dict[str, Any]] | None = None,
) -> bool:
    """把不可调和的写作冲突落盘到 novel_facts（is_conflict=1），供人工决策。

    返回是否真正插入（同内容冲突幂等）。
    """
    analysis = analysis or {}
    summary = (analysis.get("summary") or "").strip()
    if not summary and issues:
        summary = "；".join(
            f"[{item.get('category', '')}]{item.get('detail', '')}"
            for item in issues[:3]
            if item.get("detail")
        )
    if not summary:
        summary = "事实核查多次失败且不可调和"
    return novel_facts_dao.add_writing_conflict(
        project_id,
        chapter_id,
        summary,
        root_cause=analysis.get("root_cause") or "",
    )


# ── 整章重写（带问题清单） ────────────────────────────

def _build_rewrite_prompt(
    content: str,
    issues: list[dict[str, Any]],
    context_text: str,
    extra_constraints: str = "",
) -> str:
    issue_lines = "\n".join(
        f"- [{item.get('category', '')}] {item.get('detail', '')}"
        f"（修正建议：{item.get('fix', '')}）"
        for item in issues
        if item.get("detail")
    ) or "- 无具体问题"
    parts = [
        "请修订下面的章节正文，修正以下客观事实矛盾，保持情节骨架、人物性格与文风不变：",
        f"问题清单：\n{issue_lines}",
    ]
    if extra_constraints:
        parts.append(f"补充约束（必须遵守）：\n{extra_constraints}")
    if context_text:
        parts.append(f"已有故事设定参考：\n{context_text}")
    parts.append("请输出修订后的完整章节正文，不要输出解释、提纲或项目符号。")
    parts.append(f"章节正文：\n{content[:REWRITE_CONTENT_CAP]}")
    return "\n\n".join(parts)


async def rewrite_content(
    content: str,
    issues: list[dict[str, Any]],
    memory_context: dict[str, Any] | None = None,
    extra_constraints: str = "",
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> str:
    """带问题清单整章重写。LLM 失败 / 重写结果过短时降级返回原内容（不阻塞）。"""
    if not content or not content.strip():
        return content
    if allow_llm_call and not allow_llm_call("fact_rewrite", 3000):
        return content
    context_text = build_fact_check_context(memory_context)
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_rewrite_prompt(content, issues, context_text, extra_constraints),
                },
            ],
            role=ROLE_CHECK,
            max_tokens=3000,
        )
        revised = repair_text(reply).strip()
        original_len = len(_strip(content))
        revised_len = len(_strip(revised))
        if revised and revised_len >= max(int(original_len * MIN_REWRITE_KEEP_RATIO), MIN_REWRITE_LENGTH):
            return revised
    except (LLMConfigurationError, LLMResponseError):
        pass
    return content


# ── 重写编排（自检闭环） ──────────────────────────────

def _merge_issues(
    existing: list[dict[str, Any]], new_issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并去重问题清单（同 category + detail 视为重复），供重写累计注入。"""
    seen = {(item.get("category"), item.get("detail")) for item in existing}
    merged = list(existing)
    for item in new_issues:
        key = (item.get("category"), item.get("detail"))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _strip(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


async def run_fact_check_loop(
    project_id: str,
    chapter_id: str,
    content: str,
    memory_context: dict[str, Any] | None = None,
    max_retries: int = 1,
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> dict[str, Any]:
    """M3 事实核查自检闭环（重写编排）。

    流程：
      核查 → FAIL 带问题清单重写（≤max_retries 次）→ 仍失败根因分析：
        - 可调和：注入 extra_constraints 再重写一次 + 最终核查
        - 不可调和：落盘「写作冲突」标记（供人工决策，不阻塞整本）

    Codex P1-2（调用成本）：`max_retries` 默认 1——一次核查 + 一次重写 + 一次复检，
    把事实核查从「最多 9 次 LLM」压到「最多 4 次」（1 核查 + 1 重写 + 1 根因 + 1 复检）。
    调用方（自检闭环）可传更大值保留深度核查能力。

    返回：
      {
        "content": 最终正文,
        "passed": 是否最终通过,
        "stats": {"fact_check_count": 核查次数, "rewrite_count": 重写次数,
                   "escalation_count": 置信度升级复查次数},
        "conflict": 冲突分析结果（不可调和时非空）| None,
        "issues": 累计问题清单（供上层定向重写消除冲突）| [],
      }

    全程降级：LLM 任何失败返回原内容，绝不抛出异常阻塞整书任务。
    """
    stats = {"fact_check_count": 0, "rewrite_count": 0, "escalation_count": 0}
    if not content or not content.strip():
        return {"content": content, "passed": True, "stats": stats, "conflict": None, "issues": []}

    retries = max(0, int(max_retries))
    accumulated: list[dict[str, Any]] = []
    for _attempt in range(retries):
        check = await check_chapter(project_id, chapter_id, content, memory_context, allow_llm_call)
        if check.get("budget_exhausted"):
            return {"content": content, "passed": False, "status": "budget_exhausted", "stats": stats, "conflict": None, "issues": accumulated}
        stats["fact_check_count"] += 1
        stats["escalation_count"] += int(check.get("escalation_count") or 0)
        if check.get("result") != "FAIL":
            return {"content": content, "passed": True, "stats": stats, "conflict": None, "issues": []}
        accumulated = _merge_issues(accumulated, check.get("issues") or [])
        before = content
        content = await rewrite_content(content, accumulated, memory_context, allow_llm_call=allow_llm_call)
        if content == before and allow_llm_call:
            # The gate may have refused the rewrite; do not make later calls.
            return {"content": content, "passed": False, "status": "budget_exhausted", "stats": stats, "conflict": None, "issues": accumulated}
        stats["rewrite_count"] += 1

    # Codex P1-2：重写后做一次最终核查（即使 max_retries=1 也复检），
    # 通过则直接返回，避免无谓的根因分析调用。
    if stats["rewrite_count"] > 0:
        final_check = await check_chapter(project_id, chapter_id, content, memory_context, allow_llm_call)
        if final_check.get("budget_exhausted"):
            return {"content": content, "passed": False, "status": "budget_exhausted", "stats": stats, "conflict": None, "issues": accumulated}
        stats["fact_check_count"] += 1
        stats["escalation_count"] += int(final_check.get("escalation_count") or 0)
        if final_check.get("result") != "FAIL":
            return {"content": content, "passed": True, "stats": stats, "conflict": None, "issues": []}

    # 达到重试上限：根因分析
    conflict = await analyze_conflict(content, accumulated, memory_context, allow_llm_call)
    if conflict.get("budget_exhausted"):
        return {"content": content, "passed": False, "status": "budget_exhausted", "stats": stats, "conflict": None, "issues": accumulated}
    if conflict.get("reconcilable"):
        # 可调和：注入补充约束再重写一次，并做最终核查
        content = await rewrite_content(
            content, accumulated, memory_context, conflict.get("extra_constraints", ""), allow_llm_call
        )
        stats["rewrite_count"] += 1
        final_check = await check_chapter(project_id, chapter_id, content, memory_context, allow_llm_call)
        if final_check.get("budget_exhausted"):
            return {"content": content, "passed": False, "status": "budget_exhausted", "stats": stats, "conflict": None, "issues": accumulated}
        stats["fact_check_count"] += 1
        stats["escalation_count"] += int(final_check.get("escalation_count") or 0)
        if final_check.get("result") == "FAIL":
            # 补充约束后仍失败 → 保守落盘冲突
            record_writing_conflict(project_id, chapter_id, conflict, accumulated)
            return {
                "content": content,
                "passed": False,
                "stats": stats,
                "conflict": conflict,
                "issues": accumulated,
            }
        return {"content": content, "passed": True, "stats": stats, "conflict": None, "issues": []}

    # 不可调和：落盘写作冲突（不阻塞整本）
    record_writing_conflict(project_id, chapter_id, conflict, accumulated)
    return {"content": content, "passed": False, "stats": stats, "conflict": conflict, "issues": accumulated}
