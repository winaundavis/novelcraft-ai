"""去 AI 味后处理服务（建议 1：写作档位化 + 去 AI 味后处理）。

背景（调研方向 A · 不接入写作专用模型，走社区最优实践）：
- 强模型写正文 + 便宜模型辅助 + 去味润色；
- 这里实现「去味润色」一环：正文 + 项目画像的反 AI 规则目录（anti_ai_rules）
  + M3 确定性检测结果（detect_ai_artifacts）→ LLM 去 AI 味重写（ROLE_WRITING）。

降级约定（与 review_service.rewrite_opening / hook_service 一致）：
- LLM 失败（未配置 / 返回不可用）→ 返回原文，degraded=True，绝不阻塞；
- 项目无画像（无 anti_ai_rules）→ 仅用检测结果组装 prompt；
- 空文本 → 直接返回，不调用 LLM。
"""

from __future__ import annotations

from typing import Any

from app.dao import chapters as chapters_dao
from app.llm.audit import get_llm_call_meta
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.roles import ROLE_WRITING
from app.services.review_service import detect_ai_artifacts
from app.services.style_engine.profile_service import get_profile
from app.utils.text_repair import repair_text

# 单次去味处理的正文窗口上限（字）。
# 2026-10-20 修正：窗口必须与输出预算对齐。中文输出 1 字≈1 token，
# 原值「8000 字窗口 + 4000 token 预算」在长章节上必然截断。
DEAI_MAX_INPUT_CHARS = 6000
# 去味重写输出上限（token）：窗口 6000 字需要 ≈6000 token 才有写完的余量。
DEAI_MAX_TOKENS = 8192
# 重写结果长度下限（绝对）：只防空回复。
MIN_DEAI_RESULT_LENGTH = 20
# 重写结果长度保真下限（相对源文）：去味只应优化表达，不应大幅缩短。
# 2026-10-20 实测事故：6029 字正文被模型压缩成 150 字单段，旧判定
# （len >= 20）却报成功、degraded=False —— 前端会把整章替换掉，
# 作者的正文实际被删掉 97%。必须按长度保真度判定。
MIN_DEAI_RESULT_RATIO = 0.6
# 重写结果长度保真上限（相对源文）：同一次实测里模型也把 255 字扩写成 501 字（1.96 倍）。
# 去味不应新增场景与情节，超出即视为“夹带内容”，保留原文更安全。
MAX_DEAI_RESULT_RATIO = 1.5
# 台词数量允许的漂移量：去味可以改写说法，但不该新增/删除对话（那是改事实）。
DEAI_DIALOGUE_TOLERANCE = 2
# 低于这个长度的文本，幅面比例没有统计意义（短句改写经常“看起来”翻倍），
# 只做绝对长度下限判定。
DEAI_RATIO_MIN_SOURCE = 200
# 跨章检测需要的上一章正文最大长度（开头重复 / 场景词只需邻近内容）。
DEAI_PREVIOUS_TEXT_CHARS = 4000

DEAI_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


def _window_max_tokens(window: str) -> int:
    """按窗口长度估算输出预算（中文 ≈ 1 字 / 1 token，再留 1024 余量）。"""
    return max(MIN_DEAI_RESULT_LENGTH, min(DEAI_MAX_TOKENS, len(window) + 1024))


def _dialogue_marks(text: str) -> int:
    """粗略统计台词段落数（中文引号 / 直角引号 / 英文引号对）。"""
    return text.count("“") + text.count("「") + text.count('"') // 2


def _rewrite_is_acceptable(source: str, rewritten: str) -> bool:
    """保真校验：去味只能改表达，不能改篇幅结构和对话量。

    - 过短：通常意味着「被模型缩写了」或「被截断」；
    - 过长：模型自行补写场景/情节（实测 255 字被扩到 501 字）；
    - 台词数量漂移：把对话改成叙述或凭空加对白，属于改事实。
    """
    if not rewritten:
        return False
    if len(source) < DEAI_RATIO_MIN_SOURCE:
        return len(rewritten) >= MIN_DEAI_RESULT_LENGTH
    floor = max(MIN_DEAI_RESULT_LENGTH, int(len(source) * MIN_DEAI_RESULT_RATIO))
    if len(rewritten) < floor:
        return False
    if len(rewritten) > int(len(source) * MAX_DEAI_RESULT_RATIO) + MIN_DEAI_RESULT_LENGTH:
        return False
    if abs(_dialogue_marks(rewritten) - _dialogue_marks(source)) > DEAI_DIALOGUE_TOLERANCE:
        return False
    return True


def _load_previous_chapter_text(project_id: str, chapter_id: str) -> str:
    """取本章之前最近一段有正文的章节，供「开头重复 / 场景关键词组」两条跨章规则。

    未提供 chapter_id 或加载失败时返回空串，不影响去味主流程。
    """
    if not project_id or not chapter_id:
        return ""
    try:
        previous = ""
        for chapter in chapters_dao.list_chapters_global_order(project_id):
            if chapter.get("id") == chapter_id:
                break
            content = (chapter.get("content") or "").strip()
            if content:
                previous = content
        return previous[-DEAI_PREVIOUS_TEXT_CHARS:]
    except Exception:  # noqa: BLE001  加载失败不阻塞主流程
        return ""


def _load_anti_ai_rules(project_id: str) -> list[str]:
    """从项目当前生效画像读取反 AI 规则目录；无画像/无规则时返回空列表。"""
    profile = get_profile(project_id)
    if not profile:
        return []
    rules = profile.get("antiAiRules") or profile.get("anti_ai_rules") or []
    cleaned: list[str] = []
    for item in rules[:8]:
        if isinstance(item, dict):
            rule = str(item.get("rule") or "").strip()
        else:
            rule = str(item).strip()
        if rule:
            cleaned.append(rule)
    return cleaned


def _build_deai_prompt(text: str, anti_ai_rules: list[str], detection: dict) -> str:
    """组装去味 prompt：正文 + 反 AI 规则清单（可选）+ 检测结果（句式/开头/连接词/场景）。"""
    lines = [
        "请对下面的小说正文进行「去 AI 味」重写：消除机器写作痕迹，让文字读起来像人类作者手写。",
        "严格保持角色、场景、情节、对话内容与核心事实不变，只优化表达。",
    ]

    if anti_ai_rules:
        lines.append("")
        lines.append("【本项目反 AI 规则清单（必须遵守）】")
        for rule in anti_ai_rules:
            lines.append(f"- {rule}")

    issues = detection.get("issues") or []
    lines.append("")
    lines.append("【检测到的 AI 味问题（请针对性修复）】")
    if issues:
        for item in issues[:6]:
            lines.append(
                f"- {item.get('type', '问题')}：{item.get('reason', '')}"
                f"（建议：{item.get('suggestion', '')}）"
            )
    else:
        lines.append("- 未检测到明显问题；请按反 AI 规则整体提升表达的自然度与节奏。")

    lines.append("")
    lines.append("【正文】")
    lines.append(text)
    lines.append("")
    lines.append("要求：只输出重写后的完整正文，不要输出解释、标题或项目符号。")
    return "\n".join(lines)


def _split_window(text: str) -> tuple[str, str]:
    """把正文拆成（去味窗口, 尾部）。窗口内交给 LLM 重写，尾部原样保留。"""
    stripped = (text or "").strip()
    if len(stripped) <= DEAI_MAX_INPUT_CHARS:
        return stripped, ""
    return stripped[:DEAI_MAX_INPUT_CHARS], stripped[DEAI_MAX_INPUT_CHARS:]


def _split_all_windows(text: str, window_size: int = DEAI_MAX_INPUT_CHARS) -> list[str]:
    """把整章正文切成多个去味窗口（每段 ≤ window_size），保证长章节全量去味。

    段落优先在自然换行处断开（避免把一句切开）；无换行时按窗口硬切。
    """
    stripped = (text or "").strip()
    if not stripped:
        return []
    if len(stripped) <= window_size:
        return [stripped]

    windows: list[str] = []
    remaining = stripped
    while len(remaining) > window_size:
        slice_text = remaining[:window_size]
        # 在 slice 内找最后一个换行（保留段落完整性）
        newline_index = slice_text.rfind("\n")
        if newline_index > window_size * 0.6:
            cut = newline_index + 1
        else:
            cut = window_size
        windows.append(remaining[:cut])
        remaining = remaining[cut:].strip()
    if remaining:
        windows.append(remaining)
    return windows


def _build_changes(detection: dict, anti_ai_rules: list[str]) -> list[dict[str, str]]:
    """处理统计：列出本次针对哪些点做了处理（来自检测结果；无问题时给整体润色条目）。"""
    changes = [
        {
            "type": item.get("type", "问题"),
            "severity": item.get("severity", "info"),
            "note": item.get("reason", ""),
        }
        for item in (detection.get("issues") or [])
    ]
    if not changes and anti_ai_rules:
        changes.append(
            {
                "type": "整体润色",
                "severity": "info",
                "note": "未检出明显 AI 味，按反 AI 规则整体优化表达自然度。",
            }
        )
    return changes


async def deai_rewrite(project_id: str, text: str, chapter_id: str = "") -> dict[str, Any]:
    """去 AI 味后处理：正文 + 画像反 AI 规则 + 检测结果 → LLM 重写（ROLE_WRITING）。

    chapter_id 可选：提供后会加载上一章正文，让「开头重复」「场景关键词组」
    两条**跨章**规则真正参与检测（此前只传 text，这两条规则永远不会命中）。

    返回：
      {
        "result": 去味后的正文,
        "changes": 处理统计（改了哪些点）,
        "degraded": 是否降级（LLM 失败 / 结果被截断或缩写时保留原文）,
        "scoreBefore": 去味前 AI 味评分（0-100，越高越好）,
        "scoreAfter": 去味后 AI 味评分,
        "ruleCount": 注入的反 AI 规则条数,
        "issueCount": 检测出的问题条数,
        "segments": 窗口数,
        "keptWindows": 因截断/缩写而保留原文的窗口数,
      }
    """
    if not text or not text.strip():
        return {
            "result": text,
            "changes": [],
            "degraded": False,
            "scoreBefore": 100,
            "scoreAfter": 100,
            "ruleCount": 0,
            "issueCount": 0,
            "segments": 0,
            "keptWindows": 0,
        }

    anti_ai_rules = _load_anti_ai_rules(project_id)
    previous_text = _load_previous_chapter_text(project_id, chapter_id)
    detection = detect_ai_artifacts(text, previous_text)
    windows = _split_all_windows(text)

    rewritten_parts: list[str] = []
    degraded = False
    kept_windows = 0
    for index, window in enumerate(windows):
        # 多段时对每段单独检测（段落级问题更精准）；单段直接用整体检测。
        # 跨章上下文只喂给第一个窗口——后续窗口不在章节开头，比对上章开头没有意义。
        if len(windows) == 1:
            window_detection = detection
            window_previous = previous_text
        else:
            window_previous = previous_text if index == 0 else ""
            window_detection = detect_ai_artifacts(window, window_previous)
        try:
            reply = await llm.chat(
                [
                    {"role": "system", "content": DEAI_SYSTEM_PROMPT},
                    {"role": "user", "content": _build_deai_prompt(window, anti_ai_rules, window_detection)},
                ],
                role=ROLE_WRITING,
                max_tokens=_window_max_tokens(window),
            )
            finish_reason = str((get_llm_call_meta() or {}).get("finish_reason") or "")
            rewritten = repair_text(reply).strip()
            # 截断 / 大幅缩写都必须判定为失败并保留原文：
            # 否则会把「被砍掉一半的正文」当成去味成果写回项目。
            if finish_reason == "length" or not _rewrite_is_acceptable(window, rewritten):
                degraded = True
                kept_windows += 1
                rewritten_parts.append(window)
            else:
                rewritten_parts.append(rewritten)
        except (LLMConfigurationError, LLMResponseError):
            degraded = True
            kept_windows += 1
            rewritten_parts.append(window)

    result = "\n\n".join(part.strip() for part in rewritten_parts if part.strip()).strip()
    if not result:
        result = text

    score_after = detect_ai_artifacts(result).get("score", detection.get("score", 100))
    return {
        "result": result,
        "changes": _build_changes(detection, anti_ai_rules),
        "degraded": degraded,
        "scoreBefore": detection.get("score", 100),
        "scoreAfter": score_after,
        "segments": len(windows),
        "keptWindows": kept_windows,
        "ruleCount": len(anti_ai_rules),
        "issueCount": len(detection.get("issues") or []),
    }
