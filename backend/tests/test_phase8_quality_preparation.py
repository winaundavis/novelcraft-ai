import json
from pathlib import Path

import pytest

from app.services import phase8_quality_experiment_service


PRICE = {
    "currency": "CNY",
    "cache_hit_input_per_m": 0.10,
    "cache_miss_input_per_m": 3.00,
    "output_per_m": 9.00,
    "price_source": "Phase 8 fixed CNY price configuration",
    "price_checked_at": "2026-08-26T00:00:00Z",
    "price_schedule": "peak",
}


def test_phase8_quality_dry_run_is_two_calls_zero_api_and_within_budget(tmp_path):
    prepared = phase8_quality_experiment_service.prepare_quality_experiment(tmp_path, PRICE)

    assert prepared["status"] == "prepared_do_not_run"
    assert prepared["real_llm_calls"] == 0
    assert prepared["call_plan"] == {"primary_calls": 2, "retries": 0, "rewrites": 0, "qa": 0}
    assert prepared["cost_preflight"]["worst_case_cost"] <= 2.0
    manifest = json.loads(Path(prepared["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["quality_layer"] == ["Scene Embodiment", "Show/Tell"]
    assert manifest["automatic_expansion"] is False


def test_phase8_quality_dry_run_blocks_cost_above_two_cny(tmp_path):
    prepared = phase8_quality_experiment_service.prepare_quality_experiment(
        tmp_path, {**PRICE, "cache_miss_input_per_m": 1_000_000, "output_per_m": 1_000_000}
    )

    assert prepared["status"] == "blocked"
    assert prepared["cost_preflight"]["blocked"] is True


@pytest.mark.parametrize("field,value", [
    ("cache_miss_input_per_m", float("nan")),
    ("cache_miss_input_per_m", float("inf")),
    ("output_per_m", float("-inf")),
])
def test_phase8_quality_dry_run_blocks_non_finite_prices(tmp_path, field, value):
    prepared = phase8_quality_experiment_service.prepare_quality_experiment(tmp_path, {**PRICE, field: value})

    assert prepared["status"] == "blocked"
    assert prepared["cost_preflight"]["worst_case_cost"] == "unknown"
    assert prepared["cost_preflight"]["reason"] == "CNY price configuration is invalid"


def test_phase8_quality_dry_run_blocks_cost_just_over_cap_before_rounding(tmp_path):
    prepared = phase8_quality_experiment_service.prepare_quality_experiment(
        tmp_path,
        {**PRICE, "cache_miss_input_per_m": 0, "output_per_m": 833.333375},
    )

    assert prepared["cost_preflight"]["worst_case_cost"] == 2.0
    assert prepared["cost_preflight"]["blocked"] is True


def test_phase8_quality_dry_run_uses_conservative_utf8_input_bound(tmp_path):
    prepared = phase8_quality_experiment_service.prepare_quality_experiment(tmp_path, PRICE)
    requests = json.loads(Path(prepared["artifacts"]["frozen_requests"]).read_text(encoding="utf-8"))

    assert prepared["cost_preflight"]["worst_case_input_tokens"] >= sum(
        len(row["user_prompt"].encode("utf-8")) for row in requests.values()
    )
