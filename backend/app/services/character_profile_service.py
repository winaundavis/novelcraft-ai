"""按角色名分析角色档案：AI 通读**章节正文 + 大纲**后给出各字段的建议值。

设计边界（与产品确认过的冻结设计一致）：
- 只产出**建议**，绝不自动覆盖作者手写内容；是否采用由作者在前端逐项选择。
- AI 不可用或返回不合法时返回空建议 + 中文原因（HTTP 200 + degraded），
  **绝不用规则引擎或任何本地逻辑编造内容**。
- 只对「AI 有正文/大纲证据」的字段给建议；没有证据的字段直接不返回（omitted），
  由作者自己写。年龄/性别这类事实字段允许推断，但会在 `inferred` 里标出来。
- 大纲与正文进入提示词都有硬性预算，避免长篇作品把提示词撑爆。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.dao import projects as projects_dao
from app.llm.audit import reset_llm_audit_context, set_llm_audit_context
from app.llm.provider import (
    LLMConfigurationError,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
    llm,
)
from app.llm.resolver import get_runtime_provider
from app.llm.roles import ROLE_PLANNING
from app.services.character_analysis_service import load_chapters, sample_text
from app.utils.profile_text import clean_profile_text

# 大纲进入提示词的上限：最多 20 个章节节点、整段大纲文本 ≤ 3000 字符
OUTLINE_MAX_NODES = 20
OUTLINE_MAX_CHARS = 3000

# 正文进入提示词的硬性预算。
# 章节抽样（character_analysis_service.sample_text）在「章节数 ≤ 12」或「全文 ≤ 4 万字」
# 时会直接返回全文，12 章 × 长篇可能给出 18 万字；这里再做一道硬上限，
# 超限时保留开头与结尾，避免提示词被撑爆导致模型超时或输出质量下降。
PROSE_MAX_CHARS = 40_000

NO_CHAPTER_REASON = "项目还没有可分析的章节正文，请先创作或导入内容。"

# ── 字段规格：后端是唯一的字段清单来源，前端按 key 对齐 ──────────────────
_KIND_TEXT = "text"
_KIND_INT = "int"
_KIND_GENDER = "gender"

PROFILE_FIELDS: dict[str, dict[str, Any]] = {
    "personality": {
        "kind": _KIND_TEXT, "label": "性格倾向", "max": 200, "min": 10,
        "hint": "40 到 160 字的纯文字性格描述，必须包含行为倾向、压力或冲突下的反应、"
                "与人相处的方式；严禁 MBTI 四个字母、严禁性格维度名称、严禁任何数字评分或百分比",
    },
    "age": {
        "kind": _KIND_INT, "label": "年龄", "min": 1, "max": 200,
        "hint": "人物年龄的整数估计，只能给一个数字",
    },
    "gender": {
        "kind": _KIND_GENDER, "label": "性别",
        "hint": "只能是「男」「女」「未知」三者之一",
    },
    "appearance": {
        "kind": _KIND_TEXT, "label": "外貌", "max": 80,
        "hint": "一句可观察的外形特征，如身高、发型、眼神、疤痕等",
    },
    "identity": {
        "kind": _KIND_TEXT, "label": "身份", "max": 40,
        "hint": "人物在故事里的身份或职业",
    },
    "background": {
        "kind": _KIND_TEXT, "label": "背景经历", "max": 200,
        "hint": "进入故事之前的关键经历",
    },
    "coreTrauma": {
        "kind": _KIND_TEXT, "label": "核心创伤", "max": 120,
        "hint": "造成其行为模式的那次伤害、失去或错误决定",
    },
    "surfaceMotivation": {
        "kind": _KIND_TEXT, "label": "表层动机", "max": 120,
        "hint": "他自己对外承认、也愿意说出口的目标",
    },
    "deepDesire": {
        "kind": _KIND_TEXT, "label": "深层欲望", "max": 120,
        "hint": "他自己未必承认的真正渴望",
    },
    "arc": {
        "kind": _KIND_TEXT, "label": "角色弧光", "max": 150,
        "hint": "从故事开始到目前，他在立场或做法上的变化方向",
    },
    "catchphrase": {
        "kind": _KIND_TEXT, "label": "口头禅", "max": 40,
        "hint": "正文里他反复说的一类短句；文中没有就省略该字段",
    },
    "speechStyle": {
        "kind": _KIND_TEXT, "label": "说话风格", "max": 80,
        "hint": "句式长短、用词习惯、什么时候变得锋利或沉默",
    },
    "habits": {
        "kind": _KIND_TEXT, "label": "习惯动作", "max": 80,
        "hint": "紧张或思考时的可观察小动作",
    },
    "values": {
        "kind": _KIND_TEXT, "label": "价值观", "max": 80,
        "hint": "他最不肯放弃的那条原则",
    },
}

# 只能推断、正文很少明写的字段：前端会标成「AI 推断」，提醒作者过一眼再决定
INFERRED_FIELDS = ("age", "gender")

_GENDER_VALUES = ("男", "女", "未知")


def _build_profile_prompt() -> str:
    lines = [
        "你是一位资深中文小说编辑，擅长根据作品正文与大纲梳理人物档案。",
        "请针对给定角色，逐字段给出建议值。",
        "只输出一个 JSON 对象，不要输出任何其他文字、解释、标题或代码块标记，格式严格如下：",
        '{"fields":{"字段名":"建议值"}}',
        "",
        "可用的字段（键名必须原样使用）：",
    ]
    for key, spec in PROFILE_FIELDS.items():
        lines.append(f"- {key}（{spec['label']}）：{spec['hint']}")
    lines.extend([
        "",
        "硬性要求：",
        "1. 只填「正文或大纲里确实有依据」的字段。证据不足的字段**直接省略键**，"
        "不要猜测、不要用通用模板凑数。",
        "2. 严禁输出 MBTI 四个字母、严禁输出任何性格维度名称（如开放度、外向度）、"
        "严禁输出任何数字评分或百分比。",
        "3. 除 age 是纯数字之外，其余字段都是中文纯文本，不要加引号、标签或编号。",
        "4. 只描述这一个角色，不要写成旁白、提纲或分析报告。",
    ])
    return "\n".join(lines)


CHARACTER_PROFILE_SYSTEM_PROMPT = _build_profile_prompt()


class CharacterNotFoundError(LookupError):
    """项目里没有这个角色名（API 层转 404）。"""


def _extract_json(reply: str) -> dict[str, Any]:
    """容忍 ```json 围栏与前后多余文字的 JSON 提取。"""
    cleaned = (reply or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMResponseError("返回内容中未找到 JSON 对象。")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"JSON 解析失败：{exc}") from exc


def _build_outline_text(outline: Any) -> str:
    """把大纲压成有上限的纯文本；无大纲返回空串。"""
    if not isinstance(outline, dict):
        return ""

    parts: list[str] = []
    premise = str(outline.get("premise") or "").strip()
    if premise:
        parts.append(f"故事前提：{premise}")
    main_summary = str(outline.get("mainSummary") or "").strip()
    if main_summary:
        parts.append(f"总纲：{main_summary}")

    nodes = [node for node in (outline.get("nodes") or []) if isinstance(node, dict)]
    chapter_nodes = [node for node in nodes if node.get("type") == "chapter"]
    # 没有章节节点时退化为按 order 取全部节点，避免大纲信息完全丢失
    selected = chapter_nodes or nodes
    selected = sorted(selected, key=lambda node: (node.get("order") or 0, str(node.get("title") or "")))

    for node in selected[:OUTLINE_MAX_NODES]:
        title = str(node.get("title") or "").strip()
        summary = str(node.get("summary") or "").strip()
        if not title and not summary:
            continue
        parts.append(f"- {title}：{summary}" if title and summary else f"- {title or summary}")

    return "\n".join(parts).strip()[:OUTLINE_MAX_CHARS]


def _degraded(reason: str, basis: dict[str, Any]) -> dict[str, Any]:
    """没有可用建议时的统一返回（degraded + 作者能看懂的原因）。"""
    return {
        "suggestions": {},
        "rejected": {},
        "omitted": sorted(PROFILE_FIELDS),
        "inferred": [],
        "degraded": True,
        "reason": reason,
        "basis": basis,
    }


def _normalize_text_field(value: Any, spec: dict[str, Any]) -> str:
    return clean_profile_text(
        value, max_length=spec.get("max", 200), min_length=spec.get("min", 2)
    )


def _normalize_int_field(value: Any, spec: dict[str, Any]) -> tuple[int | None, str]:
    """返回 (值, 拒绝原因)。接受 32 / "32" / 32.0；其余一律拒绝。"""
    if isinstance(value, bool):
        return None, "不是有效数字"
    if isinstance(value, int):
        number: int | None = value
    elif isinstance(value, float):
        number = int(value) if float(value).is_integer() else None
        if number is None:
            return None, "不是整数"
    elif isinstance(value, str):
        matched = re.fullmatch(r"\s*(\d{1,3})\s*", value)
        if not matched:
            return None, "不是有效数字"
        number = int(matched.group(1))
    else:
        return None, "不是有效数字"

    low, high = spec.get("min", 1), spec.get("max", 200)
    if not (low <= number <= high):
        return None, f"超出合理范围（{low}-{high}）"
    return number, ""


def _normalize_gender_field(value: Any) -> tuple[str | None, str]:
    if not isinstance(value, str):
        return None, "不是文本"
    text = value.strip()
    if text in _GENDER_VALUES:
        return text, ""
    if "男" in text:
        return "男", ""
    if "女" in text:
        return "女", ""
    if "未知" in text or "无法" in text or "不明" in text:
        return "未知", ""
    return None, "只能是 男 / 女 / 未知"


def _bound_prose(text: str, limit: int = PROSE_MAX_CHARS) -> str:
    """正文侧硬性预算：超限时保留开头（60%）与结尾（40%）。

    人物的底色往往在「出场时的做派」和「最新进展里的变化」两处最明显，
    所以掐头去尾地保留两端，优于直接截断。
    """
    if len(text) <= limit:
        return text
    head_chars = int(limit * 0.6)
    tail_chars = limit - head_chars
    return f"{text[:head_chars]}\n……（正文过长，已省略中间部分）……\n{text[-tail_chars:]}"


def _provider_failure_reason(exc: Exception) -> str:
    """把 provider 异常翻译成作者能看懂的中文原因（区分场景）。"""
    if isinstance(exc, LLMConfigurationError):
        return "尚未配置可用的 AI 服务，请先到设置中添加并启用模型后再试。"
    if isinstance(exc, LLMTimeoutError):
        return "AI 服务响应超时，本次角色分析没有完成，请稍后重试。"
    if isinstance(exc, LLMTransportError):
        return "无法连接 AI 服务，请检查网络或模型服务配置后重试。"
    return "AI 服务拒绝了本次请求（返回内容无效或服务异常），请稍后重试。"


async def analyze_character_profile(project_id: str, character_name: str) -> dict[str, Any]:
    """通读章节正文与大纲，为指定角色生成各字段的建议值。

    返回 `{suggestions, rejected, omitted, inferred, degraded, reason, basis}`；
    角色不存在时抛 `CharacterNotFoundError`。
    """
    project = projects_dao.get_project(project_id) or {}
    characters = [item for item in (project.get("characters") or []) if isinstance(item, dict)]
    if character_name not in {str(item.get("name") or "").strip() for item in characters}:
        raise CharacterNotFoundError(f"该项目中没有名为「{character_name}」的角色。")

    chapters = load_chapters(project_id)
    sampled, _total_chars = sample_text(chapters)
    prose = _bound_prose(sampled)
    outline_text = _build_outline_text(project.get("outline"))
    basis = {
        "chapterCount": len(chapters),
        "sampledChars": len(prose),
        "hasOutline": bool(outline_text),
    }

    # 「有章节行」不等同于「有正文」：空章节目录在抽样后会剩下「第N章 标题」这类骨架，
    # 若只判断 sampled 是否为空就会白跑一次模型调用。
    has_prose = any(str(item.get("content") or "").strip() for item in chapters)
    if not has_prose:
        return _degraded(NO_CHAPTER_REASON, basis)

    user_prompt = (
        f"以下是小说《{str(project.get('name') or '未命名项目').strip()}》的大纲与正文节选。\n"
        f"请为角色「{character_name}」整理档案，并按要求输出 JSON。\n\n"
        f"【作品大纲】\n{outline_text or '（暂无大纲）'}\n\n"
        f"【正文节选】\n{prose}"
    )

    token = set_llm_audit_context(project_id=project_id, role=ROLE_PLANNING)
    provider = get_runtime_provider(llm)
    try:
        reply = await provider.chat(
            [
                {"role": "system", "content": CHARACTER_PROFILE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            role=ROLE_PLANNING,
        )
    except (LLMConfigurationError, LLMResponseError) as exc:
        return _degraded(_provider_failure_reason(exc), basis)
    finally:
        reset_llm_audit_context(token)

    try:
        payload = _extract_json(reply)
    except LLMResponseError:
        return _degraded("AI 返回的内容无法解析（不是有效的 JSON），请重试。", basis)

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict):
        return _degraded("AI 返回的内容缺少 fields 字段（不是约定的 JSON 结构），请重试。", basis)

    suggestions: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for key in PROFILE_FIELDS:
        if key not in raw_fields:
            continue
        spec = PROFILE_FIELDS[key]
        value = raw_fields[key]
        if spec["kind"] == _KIND_INT:
            number, why = _normalize_int_field(value, spec)
            if number is None:
                rejected[key] = why
            else:
                suggestions[key] = number
        elif spec["kind"] == _KIND_GENDER:
            gender, why = _normalize_gender_field(value)
            if gender is None:
                rejected[key] = why
            else:
                suggestions[key] = gender
        else:
            text = _normalize_text_field(value, spec)
            if not text:
                rejected[key] = "内容为空、过短，或含有 MBTI / 打分残留"
            else:
                suggestions[key] = text

    omitted = sorted(set(PROFILE_FIELDS) - set(suggestions) - set(rejected))

    if not suggestions:
        return _degraded(
            "AI 没有返回可用的字段建议（可能都缺少正文依据、或被判为格式不合法），请重试。",
            basis,
        )

    return {
        "suggestions": suggestions,
        "rejected": rejected,
        "omitted": omitted,
        "inferred": [key for key in INFERRED_FIELDS if key in suggestions],
        "degraded": False,
        "reason": None,
        "basis": basis,
    }
