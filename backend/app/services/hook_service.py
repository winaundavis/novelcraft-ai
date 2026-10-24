"""M3 钩子检测（Hook Check）—— 生成前注入钩子指令，生成后判定强钩，无钩自动重写结尾。

对应 PRD M3-②（P1-3 强钩子管理）：
- 钩子类型 6 种：悬念 / 反转 / 新信息 / 危机升级 / 情感悬置 / 场景切换预告；
- 生成前注入「本章钩子指令」+ 上一章钩子状态（先承接再埋新钩）；
- 生成后 LLM 判定结尾是否留有强钩，无则自动重写结尾（≤2 次）；
- 新表 chapter_hooks（planted → carried → resolved），与长程伏笔（P2）互补。

设计原则：
- 钩子判定只看结尾窗口（成本控制，非全文）。
- 所有 LLM 调用失败均降级（判定默认有钩 / 重写返回原文），绝不阻塞整书任务。
- prompt 为自研文案，仅借鉴参照项目的架构思想。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.dao import chapter_hooks as chapter_hooks_dao
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.roles import ESCALATION_THRESHOLD, ROLE_CHECK, ROLE_REASON
from app.utils.text_repair import repair_text

# ── 常量 ──────────────────────────────────────────────
MAX_HOOK_REWRITES = 2         # 结尾重写上限（PRD）
HOOK_ENDING_LENGTH = 400      # 结尾重写窗口（替换的结尾长度）
HOOK_CHECK_ENDING_LENGTH = 700  # 钩子判定只看结尾窗口（成本控制）
MIN_REWRITTEN_ENDING_LENGTH = 80  # 重写结尾长度下限（过短视为无效）

HOOK_TYPE_LABELS = {
    chapter_hooks_dao.HOOK_TYPE_SUSPENSE: "悬念",
    chapter_hooks_dao.HOOK_TYPE_TWIST: "反转",
    chapter_hooks_dao.HOOK_TYPE_NEW_INFO: "新信息",
    chapter_hooks_dao.HOOK_TYPE_CRISIS: "危机升级",
    chapter_hooks_dao.HOOK_TYPE_EMOTIONAL: "情感悬置",
    chapter_hooks_dao.HOOK_TYPE_SCENE_SWITCH: "场景切换预告",
}

HOOK_CHECK_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

REWRITE_ENDING_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"

RESOLUTION_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


# ── 生成前：钩子指令 ──────────────────────────────────

def build_hook_instruction(
    open_hooks: list[dict[str, Any]] | None = None,
    chapter_title: str = "",
) -> str:
    """生成前注入的「本章钩子指令」：先承接开放钩子，再埋新钩。

    open_hooks 取最近开放（planted/carried）的钩子（含上一章），由调用方
    从 chapter_hooks_dao.list_open_hooks 获取。
    """
    open_hooks = [
        h
        for h in (open_hooks or [])
        if h.get("status") in (chapter_hooks_dao.HOOK_STATUS_PLANTED, chapter_hooks_dao.HOOK_STATUS_CARRIED)
    ]
    lines = ["【本章钩子指令】"]
    if open_hooks:
        lines.append("以下钩子尚未回收（本章需承接或推进，避免遗忘）：")
        for hook in open_hooks[:5]:
            label = HOOK_TYPE_LABELS.get(hook.get("hook_type"), hook.get("hook_type") or "悬念")
            desc = hook.get("description") or "（无描述）"
            lines.append(f"- {desc}（类型：{label}）")
    lines.append(
        "章末必须留下一个强钩子——悬念/反转/新信息/危机升级/情感悬置/场景切换预告"
        "中的一种，为下一章留下明确推进动力；不要以平淡的日常场景收尾。"
    )
    return "\n".join(lines)


# ── 结尾提取/替换工具 ────────────────────────────────

def _extract_ending(content: str, length: int = HOOK_ENDING_LENGTH) -> str:
    stripped = (content or "").strip()
    if not stripped:
        return ""
    if len(stripped) <= length:
        return stripped
    return stripped[-length:]


def _replace_ending(content: str, new_ending: str) -> str:
    """保留前文，替换结尾 HOOK_ENDING_LENGTH 字。"""
    stripped = (content or "").strip()
    new_ending = (new_ending or "").strip()
    if not stripped:
        return new_ending
    if not new_ending:
        return stripped
    ending = _extract_ending(stripped, HOOK_ENDING_LENGTH)
    head = stripped[: len(stripped) - len(ending)] if ending else stripped
    return (head.rstrip() + "\n\n" + new_ending).strip()


# ── 生成后：钩子判定 ──────────────────────────────────

def _build_hook_check_prompt(content: str) -> str:
    ending = _extract_ending(content, HOOK_CHECK_ENDING_LENGTH)
    return (
        "判断下面的章节结尾是否留有**强钩子**（能驱动读者继续读下一章的元素）。\n"
        "强钩子类型（6 种）：悬念 / 反转 / 新信息 / 危机升级 / 情感悬置 / 场景切换预告。\n"
        "注意：平铺的日常收尾、把所有问题都解决干净的结尾，都不算强钩子。\n"
        "返回 JSON：{\"has_hook\": true 或 false, "
        "\"confidence\": 0.0-1.0, "
        "\"hook_type\": \"suspense|twist|new_info|crisis|emotional|scene_switch\" 或 null, "
        "\"description\": \"钩子一句话描述（无钩时简短说明为何不算强钩）\"}。\n"
        "confidence 为对判定结果的置信度：0.6 以上视为高置信；低于 0.6 表示材料不足，"
        "会触发更强模型复查。\n\n"
        f"章节结尾：\n{ending}"
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


def _parse_hook_check(reply: str) -> dict[str, Any]:
    """鲁棒解析钩子判定：优先 JSON，退化字符串匹配。

    第 3 档：解析并保留 confidence（0-1），供置信度升级链判断；缺失时默认 1.0。
    """
    if not reply or not reply.strip():
        return {"has_hook": True, "hook_type": None, "description": "", "skipped": True, "confidence": 1.0}
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
                hook_type = data.get("hook_type")
                if hook_type not in chapter_hooks_dao.VALID_HOOK_TYPES:
                    hook_type = None
                return {
                    "has_hook": bool(data.get("has_hook")),
                    "hook_type": hook_type,
                    "description": str(data.get("description") or "").strip(),
                    "skipped": False,
                    "confidence": _clamp_confidence(data.get("confidence")),
                }
        except (json.JSONDecodeError, ValueError):
            pass

    if any(word in text for word in ("无钩", "没有钩", "未留钩", "没有强钩", "无强钩")):
        return {"has_hook": False, "hook_type": None, "description": "", "skipped": False, "confidence": 1.0}
    return {"has_hook": True, "hook_type": None, "description": "", "skipped": True, "confidence": 1.0}


async def check_hook(content: str, allow_llm_call: Callable[[str, int], bool] | None = None) -> dict[str, Any]:
    """LLM 判定结尾是否留有强钩（含第 3 档置信度升级链）。

    升级链：首次用 check 档（便宜模型）判定 → 若 confidence < 阈值（默认 0.6），
    自动用 reason 档（强模型）复查一次，以强模型结果为准；失败降级返回 has_hook=True
    （跳过自检，不阻塞），升级复查失败时用原结果，绝不抛异常。
    """
    if not content or not content.strip():
        return {
            "has_hook": True, "hook_type": None, "description": "", "skipped": True,
            "confidence": 1.0, "escalated": False, "escalation_count": 0,
        }
    messages = [
        {"role": "system", "content": HOOK_CHECK_SYSTEM_PROMPT},
        {"role": "user", "content": _build_hook_check_prompt(content)},
    ]
    if allow_llm_call and not allow_llm_call("hook_check", 300):
        return {"has_hook": True, "hook_type": None, "description": "", "skipped": True, "budget_exhausted": True, "confidence": 1.0, "escalated": False, "escalation_count": 0}
    try:
        reply = await llm.chat(messages, role=ROLE_CHECK, max_tokens=300)
        result = _parse_hook_check(reply)
    except (LLMConfigurationError, LLMResponseError):
        return {
            "has_hook": True, "hook_type": None, "description": "", "skipped": True,
            "confidence": 1.0, "escalated": False, "escalation_count": 0,
        }

    # 第 3 档：低置信度 → 用 reason 档（强模型）复查一次，以强模型结果为准
    confidence = float(result.get("confidence") or 1.0)
    if confidence < ESCALATION_THRESHOLD:
        if allow_llm_call and not allow_llm_call("hook_escalation", 300):
            result.update({"budget_exhausted": True, "escalated": False, "escalation_count": 0})
            return result
        try:
            strong_reply = await llm.chat(messages, role=ROLE_REASON, max_tokens=300)
            escalated = _parse_hook_check(strong_reply)
            escalated["escalated"] = True
            escalated["escalation_count"] = 1
            return escalated
        except (LLMConfigurationError, LLMResponseError):
            # 升级复查失败 → 用原结果（降级，不抛异常）
            pass
    result["escalated"] = False
    result["escalation_count"] = 0
    return result


# ── 结尾重写 ──────────────────────────────────────────

def _build_rewrite_ending_prompt(content: str) -> str:
    ending = _extract_ending(content, HOOK_ENDING_LENGTH)
    head = content[: len(content) - len(ending)] if ending else content
    return (
        "重写下面小说章节的结尾，保留前文不变，只在结尾制造一个强钩子。\n"
        "强钩子可以是：悬念（未解之谜）、反转（意外信息）、新信息（关键线索）、"
        "危机升级（威胁迫近）、情感悬置（情绪未落地）、场景切换预告（下一场景引入）。\n"
        f"要求：新结尾约 {HOOK_ENDING_LENGTH - 100}-{HOOK_ENDING_LENGTH} 字，"
        "与上文自然衔接，不要输出标题或解释。\n\n"
        f"前文（不可改动）：\n{head[-2000:]}\n\n"
        f"当前结尾（需重写）：\n{ending}"
    )


async def rewrite_ending(content: str, allow_llm_call: Callable[[str, int], bool] | None = None) -> str:
    """重写结尾留强钩。LLM 失败 / 结果过短时降级返回原内容。"""
    if not content or not content.strip():
        return content
    if allow_llm_call and not allow_llm_call("hook_rewrite", 700):
        return content
    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": REWRITE_ENDING_SYSTEM_PROMPT},
                {"role": "user", "content": _build_rewrite_ending_prompt(content)},
            ],
            role=ROLE_CHECK,
            max_tokens=700,
        )
        new_ending = repair_text(reply).strip()
        if new_ending and len(new_ending) >= MIN_REWRITTEN_ENDING_LENGTH:
            return _replace_ending(content, new_ending)
    except (LLMConfigurationError, LLMResponseError):
        pass
    return content


# ── 上一章钩子状态更新 ────────────────────────────────

def _build_resolution_prompt(
    hook: dict[str, Any], current_content: str
) -> str:
    label = HOOK_TYPE_LABELS.get(hook.get("hook_type"), hook.get("hook_type") or "悬念")
    return (
        f"上一章埋下的钩子：{hook.get('description') or '（无描述）'}（类型：{label}）\n"
        "判断它在当前章节中是否被承接或解决（角色是否回应、悬念是否推进或揭晓、"
        "危机是否升级等）。\n"
        "返回 JSON：{\"resolved\": true 或 false}。\n\n"
        f"当前章节内容（前 2000 字）：\n{current_content[:2000]}"
    )


def _parse_resolution(reply: str) -> bool:
    if not reply or not reply.strip():
        return False
    text = reply.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end])
            if isinstance(data, dict):
                return bool(data.get("resolved"))
        except (json.JSONDecodeError, ValueError):
            pass
    return "TRUE" in text.upper() and "FALSE" not in text.upper()


async def update_previous_hooks(
    project_id: str,
    previous_chapter_id: str,
    current_content: str,
    allow_llm_call: Callable[[str, int], bool] | None = None,
) -> None:
    """判定上一章埋的钩子是否被当前章承接/解决，更新状态。

    - planted → 被本章承接但未解决 → carried；已解决 → resolved
    - carried → 被本章解决 → resolved
    LLM 失败降级为 carried（保持开放，不误判 resolved）。
    """
    hook = chapter_hooks_dao.get_hook(project_id, previous_chapter_id)
    if not hook or hook.get("status") not in (
        chapter_hooks_dao.HOOK_STATUS_PLANTED,
        chapter_hooks_dao.HOOK_STATUS_CARRIED,
    ):
        return
    if not current_content or not current_content.strip():
        return
    if allow_llm_call and not allow_llm_call("hook_resolution", 150):
        return

    try:
        reply = await llm.chat(
            [
                {"role": "system", "content": RESOLUTION_SYSTEM_PROMPT},
                {"role": "user", "content": _build_resolution_prompt(hook, current_content)},
            ],
            role=ROLE_CHECK,
            max_tokens=150,
        )
        resolved = _parse_resolution(reply)
    except (LLMConfigurationError, LLMResponseError):
        resolved = False

    new_status = (
        chapter_hooks_dao.HOOK_STATUS_RESOLVED
        if resolved
        else chapter_hooks_dao.HOOK_STATUS_CARRIED
    )
    chapter_hooks_dao.update_hook_status(project_id, previous_chapter_id, new_status)


# ── 当前章钩子落库 ────────────────────────────────────

def record_hook(
    project_id: str,
    chapter_id: str,
    check_result: dict[str, Any] | None,
    target_chapter: str = "",
) -> bool:
    """当前章埋的钩子落库（status=planted）。无钩时不落库。返回是否落库。"""
    if not check_result or not check_result.get("has_hook"):
        return False
    hook_type = check_result.get("hook_type") or chapter_hooks_dao.HOOK_TYPE_SUSPENSE
    if hook_type not in chapter_hooks_dao.VALID_HOOK_TYPES:
        hook_type = chapter_hooks_dao.HOOK_TYPE_SUSPENSE
    description = check_result.get("description") or ""
    chapter_hooks_dao.upsert_hook(
        project_id,
        chapter_id,
        hook_type=hook_type,
        status=chapter_hooks_dao.HOOK_STATUS_PLANTED,
        target_chapter=target_chapter or "",
        description=description,
    )
    return True
