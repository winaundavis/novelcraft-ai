"""全文问答（Fulltext Q&A）—— 基于 DeepSeek 前缀缓存的"偶尔全文直灌核对"。

背景（调研报告 writing-model-selection.md 建议 2）：
- DeepSeek 上下文缓存命中价 $0.0036/1M token，让"偶尔全文直灌核对"成本可忽略
  （50 万字全书直灌核对一次设定/伏笔，成本约 $0.01 量级）；
- 用户可问"某个伏笔/角色设定/前文细节在哪、是什么"，系统直灌全书 + 问题低成本回答。

稳定前缀设计（最大化 DeepSeek 前缀缓存命中）：
- prompt = 系统提示 → 全书正文 → 问题；全书正文始终在问题之前；
- 章节顺序固定为 (chapter_order, created_at) 升序（与 DAO 排序一致）；
- 同一项目、同一全文 → 相同前缀 → 后续提问只变化问题尾部，命中缓存（cache hit）。

设计原则（与 P2 伏笔账本一致）：
- LLM 失败降级：返回 degraded 提示 + 统计字段，绝不抛出异常阻塞；
- 成本控制：可选截断保护（默认 ≤600K 字，环境变量 FULLTEXT_MAX_CHARS 可覆盖）；
- 空书/无正文抛 FulltextEmptyError，由 API 层转为 400。
"""

from __future__ import annotations

import math
from typing import Any

from app.core.config import settings
from app.dao import chapters as chapters_dao
from app.dao import projects as projects_dao
from app.llm.audit import get_llm_audit_record, get_llm_call_meta
from app.llm.provider import LLMConfigurationError, LLMResponseError, llm
from app.llm.resolver import get_runtime_provider
from app.llm.roles import ROLE_FULLTEXT
from app.utils.chapter_heading import chapter_heading as compose_chapter_heading
from app.utils.chapter_heading import content_starts_with_heading

# ── 常量 ──────────────────────────────────────────────
DEFAULT_MAX_CHARS = 600_000          # 全书直灌截断保护（默认 60 万字）
MAX_ANSWER_TOKENS = 1600             # 回答输出上限（成本控制）

# 已核实价格表（USD / 1M token）。只为有明确来源的 provider+model 提供单价；
# 未列出的组合一律返回 null——不做近似推断，也不在后台自动询价。
# - deepseek：官方价格页（2026-08-27 检索）峰值价，见 docs/phase8-quality-softening-report.md。
# - arts：运营方提供的 base 价（2026-09-01，见 docs/phase10g-final-report.md）；
#   Arts **不提供**缓存命中价，因此 cache_hit_input_per_m 为 None，绝不套用 DeepSeek 官方价。
_PRICE_SCHEDULES: dict[tuple[str, str], dict[str, Any]] = {
    ("deepseek", "deepseek-v4-flash"): {
        "currency": "USD",
        "price_source": "DeepSeek official pricing page (checked 2026-08-27)",
        "cache_hit_input_per_m": 0.014,
        "cache_miss_input_per_m": 0.44,
        "output_per_m": 1.32,
    },
    ("arts", "deepseek-v4-flash"): {
        "currency": "USD",
        "price_source": "operator-supplied Arts base price (2026-09-01; weekday peak hours 2x)",
        "cache_hit_input_per_m": None,
        "cache_miss_input_per_m": 0.825,
        "output_per_m": 2.475,
    },
}

FULLTEXT_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


class FulltextEmptyError(RuntimeError):
    """全书无正文（项目不存在 / 无章节 / 章节均无内容）时抛出，API 层转为 400。"""


# ── 工具 ──────────────────────────────────────────────

def _estimate_tokens(chars: int) -> int:
    """中文正文 token 估算：1 汉字 ≈ 1 token（保守偏上，避免低估成本展示）。"""
    return max(1, math.ceil(chars))


def _resolve_provider_model(provider: Any) -> tuple[str, str]:
    """解析本次实际使用的 provider / model（只读元数据，不发起请求）。"""
    manifest: dict[str, Any] = {}
    try:
        manifest = provider.request_manifest(ROLE_FULLTEXT) or {}
    except Exception:  # noqa: BLE001  测试替身可能没有 request_manifest
        manifest = {}
    name = str(manifest.get("provider") or getattr(provider, "provider", "") or "").strip().lower()
    model = str(manifest.get("model") or getattr(provider, "model", "") or "").strip()
    return name, model


def _price_schedule(provider_name: str, model: str) -> dict[str, Any] | None:
    return _PRICE_SCHEDULES.get((provider_name, model))


def build_estimate(built: dict[str, Any], provider: Any) -> dict[str, Any]:
    """构造"估算"语义：输入/输出 token 分开，未知价格一律 null。

    - 绝不把全部输入当作缓存命中：估算按 **缓存未命中价** 取最坏情况上界
      （`basis = cache_miss_input_upper_bound`），因为估算时缓存命中情况未知。
    - 未列入价格表的 provider/model → `costUsd = null`、`status = unknown`。
    - `actual` 费用与账单无关，永远不由本函数推断。
    """
    provider_name, model = _resolve_provider_model(provider)
    schedule = _price_schedule(provider_name, model)
    input_tokens = int(built["estimated_tokens"])
    output_tokens_max = MAX_ANSWER_TOKENS

    base = {
        "inputTokens": input_tokens,
        "outputTokensMax": output_tokens_max,
        "cacheHitTokens": None,          # 估算阶段缓存命中情况未知
        "provider": provider_name,
        "model": model,
    }
    if not schedule:
        return {**base, "currency": None, "priceSource": None, "basis": None, "costUsd": None, "status": "unknown"}

    miss = schedule.get("cache_miss_input_per_m")
    out = schedule.get("output_per_m")
    if miss is None or out is None:
        return {
            **base,
            "currency": None,
            "priceSource": schedule.get("price_source"),
            "basis": None,
            "costUsd": None,
            "status": "unknown",
        }
    cost = round(input_tokens / 1_000_000 * float(miss) + output_tokens_max / 1_000_000 * float(out), 6)
    return {
        **base,
        "currency": schedule.get("currency"),
        "priceSource": schedule.get("price_source"),
        "basis": "cache_miss_input_upper_bound",
        "costUsd": cost,
        "status": "estimated",
    }


def build_actual() -> dict[str, Any]:
    """构造"实际"语义：只报告 provider 真实回传的 usage，缺失即 null。

    - 输入/输出 token 分开；provider 未回传 usage → null，不伪造成 0。
    - 缓存命中由 provider usage 决定；审计未记录即 null，不假设命中。
    - 没有 provider 账单 → `actualCostUsd` 永远 null（不做透明换算冒充账单）。
    """
    meta = get_llm_call_meta() or {}
    audit_id = str(meta.get("audit_id") or "")
    record = get_llm_audit_record(audit_id) if audit_id else None
    prompt_tokens = int((record or {}).get("prompt_tokens") or 0)
    completion_tokens = int((record or {}).get("completion_tokens") or 0)
    total_tokens = int((record or {}).get("total_tokens") or 0)
    raw_cache_hit = (record or {}).get("cache_hit_tokens")
    return {
        "provider": str(meta.get("provider") or ""),
        "model": str(meta.get("model") or ""),
        "inputTokens": prompt_tokens if prompt_tokens > 0 else None,
        "outputTokens": completion_tokens if completion_tokens > 0 else None,
        "totalTokens": total_tokens if total_tokens > 0 else None,
        # 缓存命中只取 provider 真实回传值；审计未记录 → null（不假设命中）。
        "cacheHitTokens": int(raw_cache_hit) if raw_cache_hit is not None else None,
        "currency": None,
        "actualCostUsd": None,
        "status": "reported" if total_tokens > 0 else "unavailable",
        "note": "无 provider 账单，实际费用以服务商记录为准（未做换算）。",
    }


def _chapter_heading(chapter: dict[str, Any]) -> str:
    """章节标题行（稳定格式，前缀缓存友好，便于 LLM 定位出处）。

    2026-10-18 起章节标题自带「第N章」编号（如「第3章 雨夜来客」），
    所以这里必须走 compose_chapter_heading 去重，否则会输出「## 第3章 第3章 雨夜来客」。
    """
    order = int(chapter.get("order") or 0)
    title = str(chapter.get("title") or "").strip()
    return f"## {compose_chapter_heading(order, title)}".rstrip()


# ── ① 全书组装 ───────────────────────────────────────

def build_fulltext(project_id: str, max_chars: int | None = None) -> dict[str, Any]:
    """组装全书正文：按 (chapter_order, created_at) 稳定顺序拼接，带截断保护。

    仅拼接有正文内容的章节；每章格式 `## 第N章 《标题》` + 正文（顺序稳定）。

    返回：
        {
          "fulltext": 全书正文,
          "chars": 有效字数（含标题行）,
          "chapters": 参与拼接的章节数,
          "truncated": 是否因超限截断,
          "estimated_tokens": 正文 token 估算,
        }

    项目不存在 / 无章节 / 章节均无内容 → 抛 FulltextEmptyError。
    """
    if not projects_dao.project_exists(project_id):
        raise FulltextEmptyError("项目未找到。")
    limit = max_chars or settings.fulltext_max_chars or DEFAULT_MAX_CHARS

    blocks: list[str] = []
    chars = 0
    truncated = False
    # list_chapters 已按 chapter_order + created_at 升序（DAO 层稳定排序）
    for chapter in chapters_dao.list_chapters(project_id):
        content = str(chapter.get("content") or "")
        if not content.strip():
            continue
        # 生成正文可能已经自带标题行（novel_generation_service 落库时插入）；
        # 已有就不再重复输出一行，否则导出会变成「## 第3章 雨夜来客」+「第3章 雨夜来客」。
        already_headed = content_starts_with_heading(content, chapter.get("order"), chapter.get("title"))
        heading_line = "" if already_headed else _chapter_heading(chapter) + "\n"
        block = f"{heading_line}{content}\n\n"
        if chars + len(block) > limit:
            truncated = True
            break
        blocks.append(block)
        chars += len(block)

    if not blocks:
        raise FulltextEmptyError("全书暂无正文，请先撰写章节内容。")

    return {
        "fulltext": "".join(blocks),
        "chars": chars,
        "chapters": len(blocks),
        "truncated": truncated,
        "estimated_tokens": _estimate_tokens(chars),
    }


def fulltext_stats(project_id: str, provider: Any | None = None) -> dict[str, Any]:
    """全书直灌统计（供前端展示直灌 token 与成本估算）。

    成本按实际 runtime provider/model 的已核实价格表计算；无价格来源时为 null。
    """
    built = build_fulltext(project_id)
    resolved = provider if provider is not None else get_runtime_provider(llm)
    estimate = build_estimate(built, resolved)
    return {
        "chars": built["chars"],
        "estimatedTokens": built["estimated_tokens"],
        "chapters": built["chapters"],
        "truncated": built["truncated"],
        "estimatedCostUsd": estimate["costUsd"],
        "estimate": estimate,
    }


# ── ② 全文问答 ───────────────────────────────────────

async def ask_fulltext(
    project_id: str, question: str, max_chars: int | None = None, provider: Any | None = None
) -> dict[str, Any]:
    """直灌全书 + 问题 → provider.chat(role=ROLE_FULLTEXT) → 回答。

    与 generation / style / review 一致，经 `get_runtime_provider` 解析实际 provider，
    尊重请求/任务上下文（并发 run 之间互不串线），不再直接用全局单例。

    返回：
        {
          "answer": 回答文本（provider 失败时为降级提示）,
          "estimatedTokens": 正文输入 token 估算,
          "chars" / "chapters" / "truncated": 直灌范围,
          "estimatedCostUsd": 估算费用（未知价格为 null，向后兼容字段）,
          "estimate": 结构化估算（输入/输出 token 分开、价格来源、basis、币种）,
          "actual": 结构化实际用量（provider 未回传 usage 时为 null；无账单不换算）,
          "provider" / "model": 本次解析出的实际路由,
          "degraded": provider 失败降级标志,
        }

    问题为空 / 空书 → 抛 FulltextEmptyError（API 层转 400）。
    """
    question = (question or "").strip()
    if not question:
        raise FulltextEmptyError("问题不能为空。")

    built = build_fulltext(project_id, max_chars=max_chars)
    resolved = provider if provider is not None else get_runtime_provider(llm)
    provider_name, model = _resolve_provider_model(resolved)

    # 稳定前缀：系统提示 → 全书正文 → 问题（正文始终在问题前且顺序固定）
    user_prompt = (
        "请回答以下问题（仅基于上方全书正文；若正文中找不到，请明确说明未找到）：\n"
        f"{question}"
    )

    degraded = False
    try:
        answer = await resolved.chat(
            [
                {"role": "system", "content": FULLTEXT_SYSTEM_PROMPT},
                {"role": "user", "content": built["fulltext"]},
                {"role": "user", "content": user_prompt},
            ],
            role=ROLE_FULLTEXT,
            max_tokens=MAX_ANSWER_TOKENS,
        )
    except (LLMConfigurationError, LLMResponseError):
        degraded = True
        # 失败不代表免费：无法确知 provider 是否已计费。
        answer = (
            "全文问答暂不可用：AI 服务未配置或上游请求失败，请稍后重试。"
            "请求失败时是否计费以服务商记录为准。"
        )

    estimate = build_estimate(built, resolved)
    actual = build_actual() if not degraded else {
        "provider": provider_name,
        "model": model,
        "inputTokens": None,
        "outputTokens": None,
        "totalTokens": None,
        "cacheHitTokens": None,
        "currency": None,
        "actualCostUsd": None,
        "status": "unavailable",
        "note": "请求失败：是否计费以服务商记录为准，无法确认为零费用。",
    }
    return {
        "answer": answer,
        "estimatedTokens": built["estimated_tokens"],
        "chars": built["chars"],
        "chapters": built["chapters"],
        "truncated": built["truncated"],
        "estimatedCostUsd": estimate["costUsd"],
        "estimate": estimate,
        "actual": actual,
        "provider": provider_name,
        "model": model,
        "degraded": degraded,
    }
