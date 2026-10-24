import json
from pathlib import Path

import pytest

from app.services import phase8b_quality_experiment_service


PRICE = {
    "currency": "CNY",
    "cache_hit_input_per_m": 0.10,
    "cache_miss_input_per_m": 3.00,
    "output_per_m": 9.00,
    "price_source": "Phase 8-B fixed CNY price configuration",
    "price_checked_at": "2026-08-26T00:00:00Z",
    "price_schedule": "peak",
}


def test_phase8b_preparation_is_three_calls_zero_api_and_audits_hashes(tmp_path):
    prepared = phase8b_quality_experiment_service.prepare_phase8b_quality_experiment(tmp_path, PRICE)

    assert prepared["status"] == "prepared_do_not_run"
    assert prepared["real_llm_calls"] == 0
    assert prepared["call_plan"] == {"primary_calls": 3, "retries": 0, "rewrites": 0, "qa": 0}
    assert prepared["automatic_expansion"] is False
    assert prepared["cost_preflight"]["worst_case_cost"] <= 2.0
    audit = json.loads(Path(prepared["artifacts"]["request_audit"]).read_text(encoding="utf-8"))
    assert audit["scene_hash_consistent"] is True
    assert audit["fact_hash_consistent"] is True
    assert audit["allowed_prompt_differences"] == {"A": "none", "B": "hard_quality", "C": "soft_quality_with_fact_guard"}


def test_phase8b_preparation_blocks_cost_above_two_cny(tmp_path):
    prepared = phase8b_quality_experiment_service.prepare_phase8b_quality_experiment(
        tmp_path, {**PRICE, "output_per_m": 1_000_000}
    )

    assert prepared["status"] == "blocked"
    assert prepared["cost_preflight"]["blocked"] is True


@pytest.mark.parametrize("field,value", [("output_per_m", float("nan")), ("cache_miss_input_per_m", float("inf"))])
def test_phase8b_preparation_blocks_non_finite_prices(tmp_path, field, value):
    prepared = phase8b_quality_experiment_service.prepare_phase8b_quality_experiment(tmp_path, {**PRICE, field: value})

    assert prepared["status"] == "blocked"
    assert prepared["cost_preflight"]["worst_case_cost"] == "unknown"
