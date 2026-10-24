"""Zero-call preparation for the bounded Phase 8 quality comparison."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.services.quality_gap_service import prepare_quality_gap_fixture


MAX_OUTPUT_TOKENS_PER_CALL = 1200
MAX_COST_CNY = 2.0
CALL_PLAN = {"primary_calls": 2, "retries": 0, "rewrites": 0, "qa": 0}
QUALITY_LAYER = ["Scene Embodiment", "Show/Tell"]
_REQUIRED_PRICE_FIELDS = {
    "currency",
    "price_source",
    "price_checked_at",
    "price_schedule",
    "cache_hit_input_per_m",
    "cache_miss_input_per_m",
    "output_per_m",
}


def _estimate_tokens(text: str) -> int:
    """Use UTF-8 bytes as a conservative bound for the fixed Chinese prompts."""
    return max(1, len(text.encode("utf-8")))


def _cost_preflight(price_config: dict[str, Any], requests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    worst_case_input_tokens = sum(_estimate_tokens(row["user_prompt"]) for row in requests.values())
    worst_case_output_tokens = CALL_PLAN["primary_calls"] * MAX_OUTPUT_TOKENS_PER_CALL
    base = {
        "calls": CALL_PLAN["primary_calls"],
        "retries": 0,
        "rewrites": 0,
        "qa": 0,
        "currency": "CNY",
        "worst_case_basis": "all_input_cache_miss_with_bounded_output",
        "worst_case_input_tokens": worst_case_input_tokens,
        "worst_case_output_tokens": worst_case_output_tokens,
        "max_output_tokens_per_call": MAX_OUTPUT_TOKENS_PER_CALL,
        "hard_cap": MAX_COST_CNY,
    }
    if set(price_config) < _REQUIRED_PRICE_FIELDS or price_config.get("currency") != "CNY":
        return {**base, "worst_case_cost": "unknown", "blocked": True, "reason": "CNY price configuration is incomplete or unverified"}
    try:
        input_rate = float(price_config["cache_miss_input_per_m"])
        output_rate = float(price_config["output_per_m"])
    except (TypeError, ValueError):
        return {**base, "worst_case_cost": "unknown", "blocked": True, "reason": "CNY price configuration is invalid"}
    if not all(math.isfinite(rate) and rate >= 0 for rate in (input_rate, output_rate)):
        return {**base, "worst_case_cost": "unknown", "blocked": True, "reason": "CNY price configuration is invalid"}
    raw_worst_case_cost = (
        (worst_case_input_tokens * input_rate + worst_case_output_tokens * output_rate) / 1_000_000
    )
    blocked = raw_worst_case_cost > MAX_COST_CNY
    return {
        **base,
        "worst_case_cost": round(raw_worst_case_cost, 6),
        "blocked": blocked,
        "reason": "exceeds 2 CNY hard cap" if blocked else "within 2 CNY hard cap",
    }


def prepare_quality_experiment(output_root: Path, price_config: dict[str, Any]) -> dict[str, Any]:
    """Write a zero-call Phase 8 experiment plan and its review artifacts."""
    fixture = prepare_quality_gap_fixture()
    requests = fixture["requests"]
    preflight = _cost_preflight(price_config, requests)
    status = "blocked" if preflight["blocked"] else "prepared_do_not_run"

    artifacts_root = Path(output_root) / "phase8-quality-prepared"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    requests_path = artifacts_root / "frozen_requests.json"
    requests_path.write_text(json.dumps(requests, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review_path = artifacts_root / "human_review.json"
    review_path.write_text(
        json.dumps({"questions": fixture["human_questions"], "baseline": None, "candidate": None}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result_path = artifacts_root / "result_template.json"
    result_path.write_text(
        json.dumps({"real_llm_calls": 0, "quality_signals": None, "human_review": None, "conclusion": None}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "experiment_id": "Phase-8-Quality",
        "status": status,
        "real_llm_calls": 0,
        "call_plan": CALL_PLAN,
        "quality_layer": QUALITY_LAYER,
        "automatic_expansion": False,
        "price_config": price_config,
        "cost_preflight": preflight,
    }
    manifest_path = artifacts_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "status": status,
        "real_llm_calls": 0,
        "call_plan": CALL_PLAN,
        "quality_layer": QUALITY_LAYER,
        "automatic_expansion": False,
        "cost_preflight": preflight,
        "artifacts": {
            "manifest": str(manifest_path),
            "frozen_requests": str(requests_path),
            "human_review": str(review_path),
            "result_template": str(result_path),
        },
    }
