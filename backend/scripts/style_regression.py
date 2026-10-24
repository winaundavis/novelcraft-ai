#!/usr/bin/env python
"""文笔固定回归运行框架（Codex 方案执行顺序第 9 步）。

用途：
  建立「固定匿名文笔测试集 + 回归运行框架」，让每次『模型 / 提示词 / 温度 /
  风格契约 / 自检逻辑』变更都能跑同一套测试，量化文笔变化。

运行模式：
  python scripts/style_regression.py --mock
      mock 模式：不调用 LLM。生成阶段用系统占位文本（ai_service._continue_fallback）
      验证整条管线（fixture 加载 → prompt 组装 → 生成 → 启发式评分 → AI 味检测 →
      节奏分析 → 报告输出），零成本、可离线、完全确定。
  python scripts/style_regression.py --real
      real 模式：真实调用 LLM（需 .env 配置 API key）。逐样本调用
      generate_continuation_result + check_hook + check_chapter，记录成本。
  python scripts/style_regression.py --compare <旧结果.json>
      与上次结果对比，逐样本标注每个维度升降。

对每个生成结果记录（对齐 docs/acceptance-criteria-memory-style-v1.md 3.2）：
  - heuristic_rubric_scores 10 维评分 + 加权总分（rewrite_service）
  - review_service.detect_ai_artifacts AI 味检测
  - hook_service.check_hook 章节钩子（real 模式）
  - fact_check_service.check_chapter 事实一致性（real 模式，带 memory_context）
  - 字数、耗时、fallback 标记、成本估算、按量表 6.2 的重写建议

约束：
  - 只新增 fixtures + 脚本 + 报告，不修改任何业务代码；
  - 真实生成使用独立 audit-* 项目（不落库、不写 chapters），纯内存调用；
  - LLM 失败降级，绝不抛异常中断。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_ROOT = BACKEND_DIR.parent                             # 项目根
FIXTURES_DIR = BACKEND_DIR / "tests" / "fixtures" / "style_regression"
DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "test_artifacts" / "style-regression-v1.json"
DEFAULT_REPORT_MD = PROJECT_ROOT / "docs" / "style-regression-report-v1.md"

sys.path.insert(0, str(BACKEND_DIR))

from app.models.schemas import (  # noqa: E402
    CharacterContext,
    ContinueWritingRequest,
    WorldRuleContext,
)
from app.services import (  # noqa: E402
    ai_service,
    fact_check_service,
    hook_service,
    memory_service,
    review_service,
    rewrite_service,
)

# ── 常量 ──────────────────────────────────────────────
DIMS = [f"D{i}" for i in range(1, 11)]
AUDIT_PROJECT_ID = "audit-style-regression"   # 独立审计项目，不污染真实 26 项目
PROMPT_CONTRACT_VERSION = "prompt-contract-v1"  # docs/prompt-contract-v1.md
SCHEMA_VERSION = 1

# 成本估算（中文约 1.5 字 ≈ 1 token；DeepSeek-chat 参考价，可配置）
TOKENS_PER_CHAR = 1.5
PRICE_INPUT_USD_PER_1K = 0.00027     # $0.27 / 1M tokens input
PRICE_OUTPUT_USD_PER_1K = 0.0011     # $1.10 / 1M tokens output


# ══════════════════════════════════════════════════════
# fixture 加载
# ══════════════════════════════════════════════════════

def load_fixtures(fixtures_dir: Path | None = None) -> list[dict[str, Any]]:
    """读取固定测试集（10 类 JSON × 5 样本），样本固定、版本冻结。"""
    fixtures_dir = fixtures_dir or FIXTURES_DIR
    if not fixtures_dir.exists():
        raise FileNotFoundError(
            f"测试集目录不存在: {fixtures_dir}。请确认 backend/tests/fixtures/style_regression/ 已建立。"
        )
    samples: list[dict[str, Any]] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        scene_type = data.get("scene_type") or path.stem
        label = data.get("scene_type_label") or scene_type
        version = int(data.get("version", 1))
        for s in data.get("samples", []):
            s["scene_type"] = s.get("scene_type") or scene_type
            s["scene_type_label"] = s.get("scene_type_label") or label
            s["fixture_version"] = s.get("fixture_version") or version
            samples.append(s)
    return samples


# ══════════════════════════════════════════════════════
# 请求构建
# ══════════════════════════════════════════════════════

def build_request(sample: dict[str, Any]) -> ContinueWritingRequest:
    characters = [
        CharacterContext(
            name=c.get("name", ""),
            role=c.get("role") or "",
            identity=c.get("identity") or "",
            surfaceMotivation=c.get("surfaceMotivation") or "",
            deepDesire=c.get("deepDesire") or "",
            arc=c.get("arc") or "",
            speechStyle=c.get("speechStyle") or "",
            habits=c.get("habits") or "",
        )
        for c in (sample.get("characters") or [])
    ]
    world_rules = [
        WorldRuleContext(
            name=r.get("name", ""),
            category=r.get("category") or "",
            description=r.get("description") or "",
        )
        for r in (sample.get("world_rules") or [])
    ]
    return ContinueWritingRequest(
        projectId=AUDIT_PROJECT_ID,
        chapterId=sample.get("scene_id", "unknown"),
        contextBefore=sample.get("previous_ending") or "",
        direction=sample.get("direction") or "自然抬高当前冲突，并为下一段埋下推进点。",
        wordCount=int(sample.get("target_words") or 1500),
        chapterTitle=sample.get("chapter_title") or "未命名章节",
        premise=sample.get("premise") or "",
        outlineSummary=sample.get("outline_summary") or "",
        chapterSummary=sample.get("chapter_summary") or "",
        styleTone=sample.get("style_tone") or "",
        characters=characters,
        worldRules=world_rules,
    )


def build_memory_context(sample: dict[str, Any]) -> dict[str, Any]:
    """从样本构造 fact_check 用分层记忆上下文（对齐 _build_memory_context 结构）。"""
    facts = sample.get("established_facts") or []
    return {
        "volume_summaries": [],
        "recent_chapter_summaries": [s for s in [sample.get("chapter_summary")] if s],
        "last_chapter_ending": sample.get("previous_ending") or "",
        "completed_milestones": ("\n".join(f"- {f}" for f in facts)) if facts else "",
        "entity_state": "",
        "fts_history": "",
    }


# ══════════════════════════════════════════════════════
# 生成
# ══════════════════════════════════════════════════════

async def generate(sample: dict[str, Any], req: ContinueWritingRequest, mode: str) -> dict[str, Any]:
    """生成正文。mock 用系统占位文本（真实 fallback 模板）；real 走真实 LLM。"""
    if mode == "mock":
        text = ai_service._continue_fallback(req)  # 纯规则、确定性、零 LLM
        return {"text": text, "source": "mock", "provider": "N/A", "model": "N/A (占位文本)"}
    try:
        result = await ai_service.generate_continuation_result(req)
        return {
            "text": result.get("text", ""),
            "source": result.get("source", "llm"),
            "provider": result.get("provider", ""),
            "model": result.get("model", ""),
        }
    except Exception as exc:  # LLM 失败降级，不抛异常
        return {"text": "", "source": "error", "provider": "", "model": "", "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════════════════════════════════════════
# 分析管线
# ══════════════════════════════════════════════════════

def _count_words(text: str) -> int:
    return len((text or "").replace(" ", "").replace("\n", ""))


def _norm_hook(res: Any) -> dict[str, Any]:
    if not isinstance(res, dict):
        return {"has_hook": None, "hook_type": None, "description": "", "skipped": True, "confidence": None, "note": "非 dict 结果"}
    return {
        "has_hook": res.get("has_hook"),
        "hook_type": res.get("hook_type"),
        "description": res.get("description") or "",
        "skipped": bool(res.get("skipped", False)),
        "confidence": res.get("confidence"),
        "escalated": res.get("escalated"),
        "note": res.get("note", ""),
    }


def _norm_fact(res: Any) -> dict[str, Any]:
    if not isinstance(res, dict):
        return {"result": "FAIL", "issues": [], "skipped": True, "confidence": None, "note": "非 dict 结果"}
    return {
        "result": res.get("result", "FAIL"),
        "issues": res.get("issues") or [],
        "skipped": bool(res.get("skipped", False)),
        "confidence": res.get("confidence"),
        "escalated": res.get("escalated"),
        "note": res.get("note", ""),
    }


def _compute_rewrite_suggestions(scores: dict[str, Any]) -> dict[str, Any]:
    """按评分量表 6.2 输出『应触发』的重写建议（本框架不实际重写）。"""
    dims = scores.get("dimensions", {})
    items: list[str] = []
    for dim in DIMS:
        v = dims.get(dim)
        if v is None or v > 2:
            continue
        if dim == "D10":
            items.append(f"{dim}={v}≤2 → 强制去 AI 味重写（deai_rewrite）")
        elif dim == "D9":
            items.append(f"{dim}={v}≤2 → 强制重写结尾（rewrite_ending）")
        elif dim == "D1":
            items.append(f"{dim}={v}≤2 → 整章重写（黄金三章生死线）")
        else:
            items.append(f"{dim}={v}≤2 → 局部重写建议")
    return {"count": len(items), "items": items}


def _estimate_cost(prompt: str, text: str) -> dict[str, float]:
    tokens_in = max(1, int(len(prompt or "") / TOKENS_PER_CHAR))
    tokens_out = max(1, int(len(text or "") / TOKENS_PER_CHAR))
    cost = tokens_in / 1000 * PRICE_INPUT_USD_PER_1K + tokens_out / 1000 * PRICE_OUTPUT_USD_PER_1K
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_estimated_usd": round(cost, 6)}


async def analyze(sample: dict[str, Any], text: str, mode: str, gen_meta: dict[str, Any]) -> dict[str, Any]:
    previous = sample.get("previous_ending") or ""
    rec: dict[str, Any] = {}

    # ── 纯规则（离线、零 LLM 成本）──
    rec["ai_artifacts"] = review_service.detect_ai_artifacts(text, previous)
    rec["rhythm"] = review_service._analyze_rhythm(text)
    rec["is_fallback_text"] = memory_service.is_fallback_text(text)
    rec["word_count"] = _count_words(text)

    # ── 钩子检测（D9）──
    if mode == "real" and text.strip():
        try:
            hook_res = await hook_service.check_hook(text)
        except Exception as exc:
            hook_res = {"has_hook": None, "skipped": True, "note": f"check_hook 异常: {type(exc).__name__}"}
        rec["hook"] = _norm_hook(hook_res)
    else:
        rec["hook"] = {"has_hook": None, "hook_type": None, "description": "", "skipped": True,
                        "confidence": None, "note": "mock 模式不调用 LLM"}

    # ── 事实核查（M3，带 memory_context）──
    if mode == "real" and text.strip():
        try:
            fact_res = await fact_check_service.check_chapter(
                project_id=AUDIT_PROJECT_ID,
                chapter_id=sample.get("scene_id", ""),
                content=text,
                memory_context=build_memory_context(sample),
            )
        except Exception as exc:
            fact_res = {"result": "FAIL", "issues": [], "skipped": True, "note": f"check_chapter 异常: {type(exc).__name__}"}
        rec["fact_check"] = _norm_fact(fact_res)
    else:
        rec["fact_check"] = {"result": "SKIP", "issues": [], "skipped": True, "confidence": None,
                             "note": "mock 模式不调用 LLM"}

    # ── 启发式评分（D1-D10 + 加权总分）──
    has_hook = rec["hook"].get("has_hook") if mode == "real" else None
    scores = rewrite_service.heuristic_rubric_scores(
        text,
        previous_text=previous,
        ai=rec["ai_artifacts"],
        has_hook=has_hook,
    )
    rec["scores"] = scores
    rec["suggested_rewrites"] = _compute_rewrite_suggestions(scores)
    return rec


# ══════════════════════════════════════════════════════
# 聚合
# ══════════════════════════════════════════════════════

def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    summary: dict[str, Any] = {
        "samples_run": n,
        "avg_total_score": _mean([r["scores"]["total"] for r in records]),
        "avg_word_count": _mean([float(r["word_count"]) for r in records]),
        "avg_elapsed_ms": _mean([float(r["elapsed_ms"]) for r in records]),
        "fallback_count": sum(1 for r in records if r["source"] in ("fallback", "mock")),
        "fallback_ratio": round(sum(1 for r in records if r["source"] in ("fallback", "mock")) / n, 4) if n else 0,
        "fact_error_count": sum(1 for r in records if r["fact_check"]["result"] == "FAIL" and not r["fact_check"]["skipped"]),
        "fact_skipped_count": sum(1 for r in records if r["fact_check"]["skipped"]),
        "hook_has_count": sum(1 for r in records if r["hook"].get("has_hook") is True),
        "hook_skipped_count": sum(1 for r in records if r["hook"].get("skipped")),
        "avg_ai_score": _mean([float(r["ai_artifacts"]["score"]) for r in records]),
        "ai_high_count": sum(1 for r in records if r["ai_artifacts"].get("needs_rewrite")),
        "avg_per_dimension": {d: _mean([float(r["scores"]["dimensions"][d]) for r in records]) for d in DIMS},
        "suggested_rewrite_count": sum(r["suggested_rewrites"]["count"] for r in records),
        "cost_estimated_usd_total": round(sum(r["cost"]["cost_estimated_usd"] for r in records), 6),
        # 验收 3.2 / 3.3：剧情目标完成率需人工/网文大师/LLM 评审档评定（L4），自动化留空待填
        "plot_goal_completion_rate": None,
        "cross_model_score": None,
        "master_review_score": None,
        "blind_review_result": None,
    }
    per_scene: dict[str, Any] = {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault(r["scene_type"], []).append(r)
    for st, rs in groups.items():
        per_scene[st] = {
            "samples": len(rs),
            "label": rs[0].get("scene_type_label", st),
            "avg_total": _mean([r["scores"]["total"] for r in rs]),
            "avg_word_count": _mean([float(r["word_count"]) for r in rs]),
            "fallback_count": sum(1 for r in rs if r["source"] in ("fallback", "mock")),
            "fact_error_count": sum(1 for r in rs if r["fact_check"]["result"] == "FAIL" and not r["fact_check"]["skipped"]),
            "hook_has_count": sum(1 for r in rs if r["hook"].get("has_hook") is True),
            "avg_ai_score": _mean([float(r["ai_artifacts"]["score"]) for r in rs]),
            "avg_per_dimension": {d: _mean([float(r["scores"]["dimensions"][d]) for r in rs]) for d in DIMS},
        }
    return {"summary": summary, "per_scene": per_scene}


# ══════════════════════════════════════════════════════
# 对比（--compare）
# ══════════════════════════════════════════════════════

def compare_with(records: list[dict[str, Any]], old_path: str) -> dict[str, Any]:
    old_data = json.loads(Path(old_path).read_text(encoding="utf-8"))
    old_records = old_data.get("records") or old_data.get("samples") or []
    old_by_id = {r.get("scene_id"): r for r in old_records if isinstance(r, dict)}
    new_by_id = {r.get("scene_id"): r for r in records if isinstance(r, dict)}

    rows: list[dict[str, Any]] = []
    total_deltas: dict[str, float] = {}
    for sid in sorted(set(old_by_id) | set(new_by_id)):
        if sid in old_by_id and sid in new_by_id:
            old_total = float(old_by_id[sid].get("scores", {}).get("total", 0) or 0)
            new_total = float(new_by_id[sid].get("scores", {}).get("total", 0) or 0)
            dim_deltas: dict[str, float] = {}
            for d in DIMS:
                ov = float(old_by_id[sid].get("scores", {}).get("dimensions", {}).get(d, 0) or 0)
                nv = float(new_by_id[sid].get("scores", {}).get("dimensions", {}).get(d, 0) or 0)
                dim_deltas[d] = round(nv - ov, 2)
                total_deltas[d] = total_deltas.get(d, 0.0) + dim_deltas[d]
            rows.append({
                "scene_id": sid,
                "status": "both",
                "old_total": old_total,
                "new_total": new_total,
                "total_delta": round(new_total - old_total, 2),
                "dim_deltas": dim_deltas,
            })
        else:
            rows.append({"scene_id": sid, "status": "only_new" if sid in new_by_id else "only_old"})

    n_both = sum(1 for r in rows if r["status"] == "both")
    return {
        "compared_with": old_path,
        "old_run_mode": old_data.get("run_mode"),
        "new_run_mode": records[0]["run_mode"] if records else None,
        "samples_both": n_both,
        "samples_only_new": sum(1 for r in rows if r["status"] == "only_new"),
        "samples_only_old": sum(1 for r in rows if r["status"] == "only_old"),
        "avg_total_delta": round(sum(r["total_delta"] for r in rows if r["status"] == "both") / n_both, 2) if n_both else None,
        "avg_dim_delta": {d: round(total_deltas[d] / n_both, 2) if n_both else None for d in DIMS},
        "rows": rows,
    }


# ══════════════════════════════════════════════════════
# 报告生成（Markdown + JSON）
# ══════════════════════════════════════════════════════

def _fmt_dims(dims: dict[str, Any]) -> str:
    return " ".join(f"{d}:{dims.get(d, '-')}" for d in DIMS)


def generate_report(
    meta: dict[str, Any],
    agg: dict[str, Any],
    records: list[dict[str, Any]],
    comparison: dict[str, Any] | None,
) -> str:
    summary = agg["summary"]
    per_scene = agg["per_scene"]
    lines: list[str] = []
    L = lines.append

    L("# 文笔回归报告（Style Regression Report）")
    L("")
    L("> 固定匿名文笔测试集回归运行结果。测试集：`backend/tests/fixtures/style_regression/`（10 类 × 5 样本，版本冻结）。")
    L("> 评分依据：`docs/style-rubric-v1.md`（D1-D10 + 权重）；验收对齐：`docs/acceptance-criteria-memory-style-v1.md` 3.2/3.3。")
    L("")
    L("## 一、执行概况")
    L("")
    L("| 项 | 值 |")
    L("|----|----|")
    L(f"| 运行模式 | `{meta['run_mode']}` |")
    L(f"| 生成时间 | {meta['generated_at']} |")
    L(f"| 模型 | {meta['model']} |")
    L(f"| Provider | {meta['provider']} |")
    L(f"| 温度 | {meta['temperature']} |")
    L(f"| 提示词契约版本 | {meta['prompt_version']} |")
    L(f"| 测试集版本 | {meta['fixture_versions']} |")
    L(f"| 样本总数 / 已运行 | {summary['samples_run']} |")
    L("")
    if meta["run_mode"] == "mock":
        L("> ⚠️ 本报告为 **mock 基线**：生成使用系统占位文本（`ai_service._continue_fallback`），"
          "未调用 LLM，仅验证管线正确性。hook/fact 记为 skipped。真实评分需 `--real` 运行。")
        L("")

    L("## 二、汇总指标（验收 3.2 必记字段）")
    L("")
    L("| 指标 | 值 |")
    L("|------|----|")
    L(f"| 加权总分均值（10 分制） | {summary['avg_total_score']} |")
    L(f"| 平均字数 | {summary['avg_word_count']} |")
    L(f"| 平均耗时(ms) | {summary['avg_elapsed_ms']} |")
    L(f"| fallback 比例 | {summary['fallback_count']}/{summary['samples_run']} = {summary['fallback_ratio'] * 100:.1f}% |")
    L(f"| 事实错误数（FAIL 且非 skipped） | {summary['fact_error_count']} |")
    L(f"| 事实核查 skipped | {summary['fact_skipped_count']} |")
    L(f"| 钩子命中（has_hook=true） | {summary['hook_has_count']} |")
    L(f"| 钩子判定 skipped | {summary['hook_skipped_count']} |")
    L(f"| AI 痕迹分均值（0-100） | {summary['avg_ai_score']} |")
    L(f"| AI 痕迹 high 项样本数 | {summary['ai_high_count']} |")
    L(f"| 重写建议数（量表 6.2 触发口径） | {summary['suggested_rewrite_count']} |")
    L(f"| 成本估算(USD) | {summary['cost_estimated_usd_total']}（{'估算，非真实费用' if meta['run_mode'] == 'mock' else '真实调用估算'}） |")
    L("")
    L("| 待人工/评审填写（L4） | 值 |")
    L("|--------------------------|----|")
    L(f"| 剧情目标完成率 | {summary['plot_goal_completion_rate']}（需网文大师/人工盲评，自动化留空） |")
    L(f"| 异模型评分 | {summary['cross_model_score']}（留空待填） |")
    L(f"| 网文大师评分 | {summary['master_review_score']}（留空待填） |")
    L(f"| 人工盲评结果 | {summary['blind_review_result']}（留空待填） |")
    L("")

    L("## 三、各维度均值（D1-D10）")
    L("")
    L("| 维度 | 均值(1-5) |")
    L("|------|-----------|")
    dim_labels = {
        "D1": "开篇建立", "D2": "章节有效变化", "D3": "对话推进", "D4": "角色辨识度",
        "D5": "冲突升级", "D6": "情绪三阶段", "D7": "场景具体化", "D8": "节奏变化",
        "D9": "章节钩子", "D10": "AI 痕迹",
    }
    for d in DIMS:
        L(f"| {d} {dim_labels.get(d, '')} | {summary['avg_per_dimension'][d]} |")
    L("")

    L("## 四、分场景表现")
    L("")
    L("| 场景 | 样本数 | 平均总分 | 平均字数 | AI 分 | 钩子命中 | 事实错误 | fallback |")
    L("|------|--------|----------|----------|-------|----------|----------|----------|")
    for st, ps in sorted(per_scene.items()):
        L(f"| {st} | {ps['samples']} | {ps['avg_total']} | {ps['avg_word_count']} | {ps['avg_ai_score']} | "
          f"{ps['hook_has_count']} | {ps['fact_error_count']} | {ps['fallback_count']} |")
    L("")

    L("## 五、逐样本明细")
    L("")
    L("| scene_id | 场景 | 总分 | D1 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 | AI分 | 钩子 | 事实 | 字数 | 耗时ms | 来源 |")
    L("|----------|------|------|----|----|----|----|----|----|----|----|-----|------|------|------|------|--------|------|")
    for r in sorted(records, key=lambda x: (x["scene_type"], x["scene_id"])):
        dims = r["scores"]["dimensions"]
        hook_v = r["hook"].get("has_hook")
        hook_s = "✓" if hook_v is True else ("✗" if hook_v is False else "skip")
        fact_v = r["fact_check"]["result"]
        fact_s = fact_v if not r["fact_check"]["skipped"] else "skip"
        L(f"| {r['scene_id']} | {r['scene_type']} | {r['scores']['total']} | "
          f"{dims['D1']} | {dims['D3']} | {dims['D4']} | {dims['D5']} | {dims['D6']} | "
          f"{dims['D7']} | {dims['D8']} | {dims['D9']} | {dims['D10']} | {r['ai_artifacts']['score']} | "
          f"{hook_s} | {fact_s} | {r['word_count']} | {r['elapsed_ms']:.0f} | {r['source']} |")
    L("")

    if comparison:
        L("## 六、与上次结果对比（--compare）")
        L("")
        L(f"- 对比对象：`{comparison['compared_with']}`（旧模式 {comparison['old_run_mode']} → 新模式 {comparison['new_run_mode']}）")
        L(f"- 双端共有样本：{comparison['samples_both']}；仅新版：{comparison['samples_only_new']}；仅旧版：{comparison['samples_only_old']}")
        L(f"- 总分平均变化：**{comparison['avg_total_delta']}**")
        L("")
        L("| 维度 | 平均变化 |")
        L("|------|----------|")
        for d in DIMS:
            L(f"| {d} | {comparison['avg_dim_delta'][d]} |")
        L("")
        L("| scene_id | 旧总分 | 新总分 | Δ | D1 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 |")
        L("|----------|--------|--------|----|----|----|----|----|----|----|----|-----|")
        for row in comparison["rows"]:
            if row["status"] != "both":
                L(f"| {row['scene_id']} | - | - | {row['status']} | - | - | - | - | - | - | - | - |")
                continue
            dd = row["dim_deltas"]
            L(f"| {row['scene_id']} | {row['old_total']} | {row['new_total']} | {row['total_delta']:+.2f} | "
              f"{dd['D1']:+.1f} | {dd['D3']:+.1f} | {dd['D4']:+.1f} | {dd['D5']:+.1f} | {dd['D6']:+.1f} | "
              f"{dd['D7']:+.1f} | {dd['D8']:+.1f} | {dd['D9']:+.1f} | {dd['D10']:+.1f} |")
        L("")

    L("## 七、重写建议明细（量表 6.2 触发口径，本框架不实际重写）")
    L("")
    L("| scene_id | 建议数 | 明细 |")
    L("|----------|--------|------|")
    for r in sorted(records, key=lambda x: x["scene_id"]):
        sw = r["suggested_rewrites"]
        if sw["count"]:
            L(f"| {r['scene_id']} | {sw['count']} | {'；'.join(sw['items'])} |")
    if not any(r["suggested_rewrites"]["count"] for r in records):
        L("| （无） | - | 无任何维度 ≤2 分，无需触发重写 |")
    L("")

    L("## 八、已知限制与说明")
    L("")
    if meta["run_mode"] == "mock":
        L("- **mock 占位文本评分失真**：占位文本（`_continue_fallback`）会把样本 direction 原文嵌入输出，"
          "若 direction 含量表关键词（如『禁止经过一番激战式总结』）会被启发式误判为低分。此现象仅存在于 mock 模式，"
          "真实模式（`--real`）模型不会逐字复述 direction，故 mock 分值不代表真实文笔质量，仅用于验证管线正确性。")
    L("- D2（章节有效变化）、D4（角色辨识度）为评分量表标注『必须人工』维度，启发式评分取中性 3 分，仅记债务不触发重写。")
    L("- 剧情目标完成率 / 异模型评分 / 网文大师评分 / 人工盲评结果 属于 L4 人工评审项，本框架留空待填。")
    L("- mock 模式下 hook/fact 判定 skipped（不调用 LLM）；`--real` 模式会真实调用 check_hook / check_chapter。")
    L("- 成本为按 token 估算值，仅供参考；真实费用以 `--real` 运行后 API 账单为准。")
    L("")
    L("---")
    L(f"*由 `backend/scripts/style_regression.py` 生成 · schema v{SCHEMA_VERSION} · 提示词契约 {PROMPT_CONTRACT_VERSION}*")
    return "\n".join(lines)


def save_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════

async def run_samples(
    samples: list[dict[str, Any]],
    mode: str,
    limit: int | None = None,
    scene: str | None = None,
    save_text: bool = True,
) -> list[dict[str, Any]]:
    if scene:
        samples = [s for s in samples if s["scene_type"] == scene]
    if limit:
        samples = samples[:limit]

    records: list[dict[str, Any]] = []
    for idx, sample in enumerate(samples, 1):
        scene_id = sample["scene_id"]
        print(f"[{idx}/{len(samples)}] {mode} 模式 · {sample['scene_type']} · {scene_id} ...", flush=True)
        try:
            req = build_request(sample)
            prompt = ai_service._build_continue_prompt(req)
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]

            t0 = time.perf_counter()
            gen = await generate(sample, req, mode)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            analysis = await analyze(sample, gen["text"], mode, gen)
            cost = _estimate_cost(prompt, gen["text"])

            record: dict[str, Any] = {
                "scene_id": scene_id,
                "scene_type": sample["scene_type"],
                "scene_type_label": sample.get("scene_type_label", sample["scene_type"]),
                "fixture_version": sample.get("fixture_version", 1),
                "run_mode": mode,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "prompt_version": PROMPT_CONTRACT_VERSION,
                "prompt_hash": prompt_hash,
                "model": gen.get("model", ""),
                "provider": gen.get("provider", ""),
                "source": gen.get("source", ""),
                "error": gen.get("error", ""),
                "elapsed_ms": round(elapsed_ms, 2),
                "cost": cost,
                **analysis,
            }
            if save_text:
                record["text"] = gen["text"]
            records.append(record)
        except Exception as exc:
            # 样本级失败降级：记录错误，不中断整轮
            print(f"  !! {scene_id} 处理失败，降级记录: {type(exc).__name__}: {exc}", flush=True)
            records.append({
                "scene_id": scene_id,
                "scene_type": sample.get("scene_type", ""),
                "scene_type_label": sample.get("scene_type_label", ""),
                "fixture_version": sample.get("fixture_version", 1),
                "run_mode": mode,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "prompt_version": PROMPT_CONTRACT_VERSION,
                "prompt_hash": "",
                "model": "", "provider": "", "source": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": 0.0,
                "cost": {"tokens_in": 0, "tokens_out": 0, "cost_estimated_usd": 0.0},
                "word_count": 0,
                "scores": {"dimensions": {d: 1 for d in DIMS}, "total": 1.0, "method": "heuristic"},
                "ai_artifacts": {"score": 0, "needs_rewrite": False, "debt_items": [], "issues": []},
                "rhythm": {"score": 0, "issues": []},
                "is_fallback_text": False,
                "hook": {"has_hook": None, "hook_type": None, "skipped": True, "note": "样本处理失败"},
                "fact_check": {"result": "SKIP", "issues": [], "skipped": True, "note": "样本处理失败"},
                "suggested_rewrites": {"count": 0, "items": []},
            })
    return records


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="文笔固定回归运行框架（NovelCraft AI · Codex 第 9 步）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/style_regression.py --mock                # mock 基线，验证管线（默认）\n"
            "  python scripts/style_regression.py --real                # 真实 LLM，建立 v1 基线\n"
            "  python scripts/style_regression.py --compare test_artifacts/style-regression-v1.json  # 与上次对比\n"
            "  python scripts/style_regression.py --mock --limit 10     # 只跑前 10 个样本\n"
            "  python scripts/style_regression.py --mock --scene dialogue  # 只跑某类场景"
        ),
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--mock", action="store_true", help="mock 模式：不调 LLM，用占位文本验证管线")
    g.add_argument("--real", action="store_true", help="real 模式：真实调用 LLM（需配置 API key）")
    parser.add_argument("--compare", metavar="OLD_JSON", help="与上次结果 JSON 对比，标注每维度升降")
    parser.add_argument("--limit", type=int, default=None, help="只运行前 N 个样本")
    parser.add_argument("--scene", default=None, help="只运行指定 scene_type（如 dialogue）")
    parser.add_argument("--fixtures", default=None, help="自定义测试集目录（默认 backend/tests/fixtures/style_regression）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_JSON), help="结果 JSON 输出路径")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_MD), help="Markdown 报告输出路径")
    parser.add_argument("--no-save-text", action="store_true", help="不把生成文本写入结果 JSON（减小体积）")
    return parser.parse_args(argv)


def _resolve_path(p: str | None) -> Path | None:
    """相对路径统一基于项目根解析（与默认输出路径一致），绝对路径原样使用。"""
    if not p:
        return None
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "real" if args.real else "mock"

    fixtures_dir = _resolve_path(args.fixtures)
    samples = load_fixtures(fixtures_dir) if fixtures_dir else load_fixtures()
    if args.scene:
        filtered = [s for s in samples if s["scene_type"] == args.scene]
        print(f"按场景筛选：{args.scene} → {len(filtered)} 个样本")
        samples = filtered
    total_fixture = len(samples)
    print(f"固定测试集加载完成：{total_fixture} 个样本，模式={mode}")

    records = asyncio.run(run_samples(samples, mode, args.limit, None, save_text=not args.no_save_text))
    if not records:
        print("无样本可运行，退出。")
        return 1

    agg = aggregate(records)
    meta = {
        "run_mode": mode,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": records[0]["model"] if records else "N/A",
        "provider": records[0]["provider"] if records else "N/A",
        "temperature": None if mode == "mock" else "0.8（默认，未显式设置）",
        "prompt_version": PROMPT_CONTRACT_VERSION,
        "fixture_versions": sorted({str(s.get("fixture_version", 1)) for s in samples}),
        "schema_version": SCHEMA_VERSION,
    }

    comparison = None
    if args.compare:
        try:
            comparison = compare_with(records, str(_resolve_path(args.compare)))
        except Exception as exc:
            print(f"!! 对比失败（不影响本轮结果）: {type(exc).__name__}: {exc}")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_mode": mode,
        "generated_at": meta["generated_at"],
        "meta": meta,
        "fixture_total": total_fixture,
        "summary": agg["summary"],
        "per_scene": agg["per_scene"],
        "comparison": comparison,
        "records": records,
    }

    output_json = _resolve_path(args.output) or DEFAULT_OUTPUT_JSON
    save_json(payload, output_json)

    report_md = generate_report(meta, agg, records, comparison)
    report_path = _resolve_path(args.report) or DEFAULT_REPORT_MD
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_md, encoding="utf-8")

    s = agg["summary"]
    print("=" * 72)
    print(f"文笔回归完成（{mode}）")
    print(f"  样本: {s['samples_run']} | 平均总分: {s['avg_total_score']} | 平均字数: {s['avg_word_count']}")
    print(f"  fallback: {s['fallback_count']} ({s['fallback_ratio']*100:.1f}%) | 事实错误: {s['fact_error_count']} | AI分均值: {s['avg_ai_score']}")
    print(f"  重写建议: {s['suggested_rewrite_count']} | 成本估算: ${s['cost_estimated_usd_total']}")
    print(f"  JSON → {output_json}")
    print(f"  报告 → {report_path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
