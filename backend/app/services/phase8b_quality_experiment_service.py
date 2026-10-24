"""Zero-call preparation and cost guard for the Phase 8-B A/B/C fixture."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.services.phase8b_quality_service import prepare_phase8b_fixture


MAX_OUTPUT_TOKENS_PER_CALL = 1200
MAX_COST_CNY = 2.0
CALL_PLAN = {"primary_calls": 3, "retries": 0, "rewrites": 0, "qa": 0}
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
    return max(1, len(text.encode("utf-8")))


def _cost_preflight(price_config: dict[str, Any], requests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    input_tokens = sum(_estimate_tokens(row["user_prompt"]) for row in requests.values())
    output_tokens = CALL_PLAN["primary_calls"] * MAX_OUTPUT_TOKENS_PER_CALL
    base = {
        "calls": CALL_PLAN["primary_calls"],
        "retries": 0,
        "rewrites": 0,
        "qa": 0,
        "currency": "CNY",
        "worst_case_basis": "all_input_cache_miss_with_bounded_output",
        "worst_case_input_tokens": input_tokens,
        "worst_case_output_tokens": output_tokens,
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
    raw_cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return {
        **base,
        "worst_case_cost": round(raw_cost, 6),
        "blocked": raw_cost > MAX_COST_CNY,
        "reason": "exceeds 2 CNY hard cap" if raw_cost > MAX_COST_CNY else "within 2 CNY hard cap",
    }


def _request_audit(requests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scene_hashes = {row["scene_hash"] for row in requests.values()}
    fact_hashes = {row["fact_hash"] for row in requests.values()}
    return {
        "variants": list(requests),
        "scene_hash": next(iter(scene_hashes)) if len(scene_hashes) == 1 else None,
        "fact_hash": next(iter(fact_hashes)) if len(fact_hashes) == 1 else None,
        "scene_hash_consistent": len(scene_hashes) == 1,
        "fact_hash_consistent": len(fact_hashes) == 1,
        "allowed_prompt_differences": {variant: row["quality_layer"] for variant, row in requests.items()},
        "real_llm_calls": 0,
    }


def prepare_phase8b_quality_experiment(output_root: Path, price_config: dict[str, Any]) -> dict[str, Any]:
    """Write frozen A/B/C requests and review templates without a provider call."""
    fixture = prepare_phase8b_fixture()
    requests = fixture["requests"]
    preflight = _cost_preflight(price_config, requests)
    status = "blocked" if preflight["blocked"] else "prepared_do_not_run"
    artifacts_root = Path(output_root) / "phase8b-quality-prepared"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    requests_path = artifacts_root / "frozen_requests.json"
    requests_path.write_text(json.dumps(requests, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path = artifacts_root / "request_audit.json"
    audit_path.write_text(json.dumps(_request_audit(requests), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review_path = artifacts_root / "human_review.json"
    review_path.write_text(
        json.dumps(
            {"questions": fixture["human_questions"], "allowed_answers": ["A", "B", "C", "平手", "都不够"], "answers": None},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    result_path = artifacts_root / "result_template.json"
    result_path.write_text(
        json.dumps({"real_llm_calls": 0, "fact_violations": None, "lore_inventions": None, "pronoun_consistency": None, "human_review": None}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "experiment_id": "Phase-8B-Quality-Softening",
        "status": status,
        "real_llm_calls": 0,
        "call_plan": CALL_PLAN,
        "quality_layer": {"B": "Hard Quality", "C": "Soft Quality + Canonical Fact Guard"},
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
        "automatic_expansion": False,
        "cost_preflight": preflight,
        "artifacts": {
            "manifest": str(manifest_path),
            "frozen_requests": str(requests_path),
            "request_audit": str(audit_path),
            "human_review": str(review_path),
            "result_template": str(result_path),
        },
    }
