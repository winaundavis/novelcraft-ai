"""Historical Pilot-0 runner; not the Experiment 001 Harness A/B runner.

This retained script produced Pilot-0's historical six calls. It is intentionally bounded: three chapters per variant, one request
per chapter, no automatic retries, and a hard token/cost preflight. It writes
only experiment artifacts; it does not mutate a NovelCraft project.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import get_active_llm_config, get_conn
from app.llm.audit import query_llm_audit, reset_llm_audit_context, set_llm_audit_context
from app.llm.provider import LLMProvider


PROJECT_ID = "5af405dcfa02"
VARIANTS = ("baseline", "candidate")
CHAPTERS = 3
TARGET_WORDS_PER_CHAPTER = 1100
MAX_TOKENS_PER_CALL = 1600
INPUT_PRICE_PEAK_CNY_PER_M = 1.65
OUTPUT_PRICE_PEAK_CNY_PER_M = 4.95
HARD_INPUT_TOKENS = 200_000
HARD_OUTPUT_TOKENS = 20_000
MAX_BUDGET_USD = 10.0


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {} if value in (None, "") else value


def load_fixture(project_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        project = conn.execute(
            "SELECT id, name, premise, outline, characters, world_rules FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    if not project:
        raise RuntimeError(f"project not found: {project_id}")
    outline = _json(project["outline"])
    nodes = [node for node in (outline.get("nodes") or []) if node.get("type") == "chapter"]
    nodes = nodes[:CHAPTERS]
    if len(nodes) != CHAPTERS:
        raise RuntimeError("selected project does not contain three chapter outline nodes")
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "premise": project["premise"] or outline.get("mainSummary") or "",
        "outline_summary": outline.get("mainSummary") or "",
        "outline_nodes": nodes,
        "characters": _json(project["characters"]) if project["characters"] else [],
        "world_rules": _json(project["world_rules"]) if project["world_rules"] else [],
    }


def _estimate(prompt_chars: int) -> dict[str, float]:
    input_tokens = min(max(prompt_chars, 1), HARD_INPUT_TOKENS)
    output_tokens = CHAPTERS * len(VARIANTS) * MAX_TOKENS_PER_CALL
    peak_cny = input_tokens / 1_000_000 * INPUT_PRICE_PEAK_CNY_PER_M + output_tokens / 1_000_000 * OUTPUT_PRICE_PEAK_CNY_PER_M
    return {
        "input_tokens_cap": float(input_tokens),
        "output_tokens_cap": float(output_tokens),
        "peak_price_cny": round(peak_cny, 6),
        "price_basis": "daytime_peak = 2x supplied night price",
        "usd_budget_gate": MAX_BUDGET_USD,
    }


def _prompt(fixture: dict[str, Any], variant: str, chapter: dict[str, Any], prior: list[str]) -> list[dict[str, str]]:
    fixed = (
        f"故事 premise：{fixture['premise']}\n"
        f"固定大纲：{fixture['outline_summary']}\n"
        f"本章目标：{chapter.get('summary') or chapter.get('title')}\n"
        f"人物卡（固定）：{json.dumps(fixture['characters'], ensure_ascii=False)}\n"
        f"前文（本变体已生成内容）：{chr(10).join(prior[-2:]) or '无'}\n"
        f"请生成第 {chapter.get('order')} 章，约 {TARGET_WORDS_PER_CHAPTER} 字，只输出正文。"
    )
    if variant == "baseline":
        return [{"role": "system", "content": "你是中文网络小说作者。遵守给定大纲，保持叙事连贯。"}, {"role": "user", "content": fixed}]
    candidate_context = (
        fixed
        + "\nNovelCraft harness context（仅 candidate 变量）：\n"
        + "- 使用结构化章节合同、前文状态、未解决问题和下一步方向。\n"
        + "- 不新增人物事实；没有证据的内容保持为未确认。\n"
        + "- 结尾留下可由下一章承接的明确 hook。"
    )
    return [{"role": "system", "content": "你是 NovelCraft harness 中的中文网络小说作者。严格依据结构化上下文写作。"}, {"role": "user", "content": candidate_context}]


async def run(project_id: str, output_dir: Path) -> dict[str, Any]:
    if os.getenv("RUN_LIVE_LLM") != "1":
        raise RuntimeError("set RUN_LIVE_LLM=1 explicitly to authorize real calls")
    cfg = get_active_llm_config() or {}
    provider_name, model = str(cfg.get("provider") or ""), str(cfg.get("model") or "")
    if not provider_name or not model or not cfg.get("apiKey"):
        raise RuntimeError("active provider/model/api key is incomplete")
    fixture = load_fixture(project_id)
    prompt_chars = sum(len(json.dumps(_prompt(fixture, variant, node, []), ensure_ascii=False)) for variant in VARIANTS for node in fixture["outline_nodes"])
    estimate = _estimate(prompt_chars)
    # The supplied prices are CNY; this gate is deliberately expressed in CNY
    # and is far below any reasonable CNY equivalent of the USD 10 ceiling.
    if estimate["peak_price_cny"] > 5.0:
        raise RuntimeError(f"conservative peak estimate exceeds local safety cap: {estimate}")

    provider = LLMProvider(provider=provider_name, model=model)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results: dict[str, Any] = {"experiment_id": "Experiment-001", "run_id": run_id, "status": "running", "project": fixture, "provider": provider_name, "model": model, "estimate": estimate, "variants": {}}
    for variant in VARIANTS:
        prior: list[str] = []
        chapters: list[dict[str, Any]] = []
        for chapter in fixture["outline_nodes"]:
            token = set_llm_audit_context(project_id=project_id, role="writing", chapter_id=str(chapter.get("id") or ""), operation=f"Experiment-001/{variant}", merge=False)
            started = time.monotonic()
            try:
                text = await provider.chat(_prompt(fixture, variant, chapter, prior), role="writing", temperature=0.8, max_tokens=MAX_TOKENS_PER_CALL)
                text = str(text or "").strip()
                chapters.append({"chapter_id": chapter.get("id"), "title": chapter.get("title"), "text": text, "chars": len(text), "elapsed_s": round(time.monotonic() - started, 3)})
                prior.append(text)
            finally:
                reset_llm_audit_context(token)
        results["variants"][variant] = {"chapters": chapters, "total_chars": sum(c["chars"] for c in chapters)}
    audit = query_llm_audit(project_id=project_id, limit=1000)
    experiment_audit = [row for row in audit if str(row.get("operation") or "").startswith("Experiment-001/")]
    results["audit"] = experiment_audit
    results["actual"] = {
        "calls": len(experiment_audit),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in experiment_audit),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in experiment_audit),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in experiment_audit),
    }
    results["status"] = "completed" if results["actual"]["calls"] == CHAPTERS * len(VARIANTS) else "incomplete"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"experiment-001-{run_id}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    results["artifact"] = str(path)
    return results


def prepare(project_id: str, output_dir: Path) -> dict[str, Any]:
    fixture = load_fixture(project_id)
    prompt_chars = sum(len(json.dumps(_prompt(fixture, variant, node, []), ensure_ascii=False)) for variant in VARIANTS for node in fixture["outline_nodes"])
    cfg = get_active_llm_config() or {}
    manifest = {
        "experiment_id": "Experiment-001",
        "status": "prepared_do_not_run",
        "project": fixture,
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "temperature": 0.8,
        "max_tokens_per_call": MAX_TOKENS_PER_CALL,
        "chapters_per_variant": CHAPTERS,
        "target_words_per_chapter": TARGET_WORDS_PER_CHAPTER,
        "variants": {"baseline": "direct prompt", "candidate": "NovelCraft harness context"},
        "only_changed_variable": "harness",
        "estimate": _estimate(prompt_chars),
        "characters_note": "project has no explicit character cards; both variants use the same empty fixed character list",
        "real_llm_calls": 0,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "experiment-001-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["artifact"] = str(path)
    return manifest


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=PROJECT_ID)
    parser.add_argument("--output-dir", default="test_artifacts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = prepare(args.project, Path(args.output_dir)) if args.dry_run else asyncio.run(run(args.project, Path(args.output_dir)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
