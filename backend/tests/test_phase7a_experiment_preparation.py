import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.experiment_001_service import (
    FIXTURE_ROOT,
    ExperimentPreparationError,
    experiment_001_readiness,
    prepare_experiment_001,
)
from app.services.experiment_schema import HumanReview, REVIEW_DIMENSIONS


def _cny_price(**overrides):
    return {
        "currency": "CNY",
        "price_source": "test-price-source",
        "price_checked_at": "2026-08-24T00:00:00Z",
        "price_schedule": "peak",
        "cache_hit_input_per_m": 0.10,
        "cache_miss_input_per_m": 3.00,
        "output_per_m": 9.00,
        **overrides,
    }


def test_fixture_is_complete_and_frozen():
    required = {"story.json", "characters.json", "world_rules.json", "outline.json", "story_state.json", "chapter_contracts.json", "active_foreshadows.json", "style_profile.json", "recent_history.json"}
    assert required == {path.name for path in FIXTURE_ROOT.iterdir()}
    characters = json.loads((FIXTURE_ROOT / "characters.json").read_text(encoding="utf-8"))
    assert len(characters) == 5
    assert all({"identity", "goal", "personality", "relationships", "speech_habits", "current_state"} <= set(item) for item in characters)
    assert len(json.loads((FIXTURE_ROOT / "world_rules.json").read_text(encoding="utf-8"))) >= 5
    outline = json.loads((FIXTURE_ROOT / "outline.json").read_text(encoding="utf-8"))
    assert len(outline["chapters"]) == 3 and all(len(item["objective"]) > 30 for item in outline["chapters"])


def test_prepare_uses_isolated_snapshot_and_freezes_real_harness(tmp_path):
    from app import db
    from app.services import project_context_service
    before = hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest()
    with patch.object(project_context_service, "assemble_project_context", wraps=project_context_service.assemble_project_context) as assemble:
        result = prepare_experiment_001(tmp_path, provider="mock", model="mock-model", price_config=_cny_price())
    assert assemble.call_count == 3
    assert before == hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest()
    assert result["real_llm_calls"] == 0
    assert result["experiment_namespace"].startswith("experiment-001-")
    assert Path(result["snapshot_db"]).parent.parent == tmp_path / result["experiment_namespace"]
    assert Path(result["snapshot_db"]).exists()  # Retained only inside the experimental namespace root.
    candidate = result["requests"]["candidate"]
    assert len(candidate) == 3
    assert all("NovelCraft" in row["compiled_context"] and row["prompt_hash"] and row["context_hash"] for row in candidate)
    assert all(row["system_prompt"] == "你是一位中文连载小说作者。\n只返回本章中文正文，不要解释。\n正文和专有名词全部使用中文，不得出现英文单词或拉丁字母。" for row in candidate + result["requests"]["baseline"])
    assert all({"story_hash", "character_hash", "world_hash", "outline_hash", "chapter_target_hash"} <= set(row) for row in candidate)
    assert all(row["context_chars"] > 0 and row["estimated_context_tokens"]["approximate"] for row in candidate)
    assert all(row["system_prompt"] and row["user_prompt"] for row in candidate)
    assert not result["input_audit"]["blocked"]
    assert Path(result["artifacts"]["blind_review"]).exists()
    blind = Path(result["artifacts"]["blind_review"]).read_text(encoding="utf-8").lower()
    assert not any(word in blind for word in ("baseline", "candidate", "provider", "model", "harness"))
    assert Path(result["artifacts"]["blind_mapping"]).exists()
    audit = json.loads(Path(result["artifacts"]["context_audit"]).read_text(encoding="utf-8"))
    assert all(row["injected"]["memory"] and row["injected"]["chapter_contract"] for row in audit["chapters"])
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["production_database_hash_before"] == manifest["production_database_hash_after"]
    assert manifest["harness_version_hash"] and manifest["runner_version_hash"]


def test_fixed_input_audit_rejects_non_harness_difference(tmp_path):
    with pytest.raises(ExperimentPreparationError, match="non-harness"):
        prepare_experiment_001(tmp_path, provider="mock", model="mock-model", price_config=_cny_price(), baseline_overrides={"temperature": 0.9})


def test_budget_blocks_unknown_or_over_target(tmp_path):
    unknown = prepare_experiment_001(tmp_path, provider="mock", model="mock-model")
    assert unknown["status"] == "blocked" and unknown["cost_preflight"]["worst_case_cost"] == "unknown"
    over = prepare_experiment_001(tmp_path, provider="mock", model="mock-model", price_config=_cny_price(cache_miss_input_per_m=1_000_000, output_per_m=1_000_000))
    assert over["status"] == "blocked" and over["cost_preflight"]["blocked"]


def test_preparation_records_cny_peak_cache_miss_cost_gate(tmp_path):
    result = prepare_experiment_001(
        tmp_path,
        provider="DeepSeek",
        model="deepseek-v4-flash",
        price_config={
            "currency": "CNY",
            "price_source": "user-provided Phase 7-C task specification",
            "price_checked_at": "2026-08-24T00:00:00Z",
            "price_schedule": "peak",
            "cache_hit_input_per_m": 0.10,
            "cache_miss_input_per_m": 3.00,
            "output_per_m": 9.00,
        },
        thinking_mode="low",
        reasoning_effort="low",
    )

    preflight = result["cost_preflight"]
    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    assert result["real_llm_calls"] == 0
    assert result["status"] == "prepared_do_not_run"
    assert preflight["currency"] == "CNY"
    assert preflight["worst_case_basis"] == "cache_miss_input"
    assert preflight["target_cost"] == 5.0
    assert preflight["hard_cap"] == 20.0
    assert preflight["single_call_cap"] == 2.0
    assert preflight["worst_case_cost"] <= 5.0
    assert manifest["price_config"]["price_schedule"] == "peak"
    assert manifest["fixed_parameters"]["thinking_mode"] == "low"
    assert manifest["fixed_parameters"]["reasoning_effort"] == "low"


def test_review_schema_has_twelve_dimensions_and_reason():
    assert len(REVIEW_DIMENSIONS) == 12
    review = HumanReview(*(3 for _ in REVIEW_DIMENSIONS), reason="clear")
    assert review.reason == "clear" and review.overall_preference == 3


def test_frozen_requests_require_explicit_finalize_to_change(tmp_path):
    result = prepare_experiment_001(tmp_path, provider="mock", model="one")
    frozen = Path(result["artifacts"]["frozen_requests"])
    frozen.write_text(frozen.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ExperimentPreparationError, match="explicit finalize"):
        prepare_experiment_001(tmp_path, provider="mock", model="one")


def test_readiness_marker_blocks_unknown_cost_and_is_read_only(tmp_path):
    prepare_experiment_001(tmp_path, provider="mock", model="mock-model")
    readiness = experiment_001_readiness(tmp_path, core_tests_pass=True, packaging_pass=True, harness_spy_pass=True, default_suite_zero_api=True)
    assert readiness["status"] == "BLOCKED"
    assert readiness["blocking_reasons"] == ["price_source_missing", "cost_gate_blocked"]


def test_readiness_accepts_complete_cny_preflight_artifacts(tmp_path):
    prepare_experiment_001(tmp_path, provider="DeepSeek", model="deepseek-v4-flash", price_config=_cny_price())
    readiness = experiment_001_readiness(tmp_path, core_tests_pass=True, packaging_pass=True, harness_spy_pass=True, default_suite_zero_api=True)
    assert readiness["status"] == "READY_TO_RUN"
