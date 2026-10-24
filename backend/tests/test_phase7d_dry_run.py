import hashlib
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from app import db


def _price(**overrides):
    return {
        "currency": "CNY",
        "price_source": "Phase 7-D fixed price configuration",
        "price_checked_at": "2026-08-25T00:00:00Z",
        "price_schedule": "peak",
        "cache_hit_input_per_m": 0.10,
        "cache_miss_input_per_m": 3.00,
        "output_per_m": 9.00,
        **overrides,
    }


def test_phase7d_dry_run_freezes_twenty_call_continuity_plan(tmp_path):
    from app.services import phase7d_experiment_service as service

    production_before = hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest()
    with patch.object(service.project_context_service, "assemble_project_context", wraps=service.project_context_service.assemble_project_context) as assemble:
        result = service.prepare_phase7d_experiment(
            tmp_path,
            provider="DeepSeek",
            model="deepseek-v4-flash",
            price_config=_price(),
        )

    assert result["status"] == "prepared_do_not_run"
    assert result["real_llm_calls"] == 0
    assert result["call_plan"]["primary_calls"] == 20
    assert result["call_plan"]["retries"] == 0
    assert result["call_plan"]["rewrites"] == 0
    assert result["call_plan"]["qa"] == 0
    assert assemble.call_count == 10
    assert production_before == hashlib.sha256(Path(db.DB_PATH).read_bytes()).hexdigest()

    manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text(encoding="utf-8"))
    frozen = json.loads(Path(result["artifacts"]["frozen_inputs"]).read_text(encoding="utf-8"))
    plan = json.loads(Path(result["artifacts"]["request_plan"]).read_text(encoding="utf-8"))

    assert len(frozen["outline"]["chapters"]) == 10
    assert len(frozen["chapter_contracts"]) == 10
    assert set(frozen) == {
        "story", "characters", "world", "outline", "style", "initial_story_state",
        "initial_memory", "initial_foreshadows", "chapter_contracts", "chapter_objectives",
    }
    assert manifest["frozen_input_hash"]
    assert len(manifest["run_order"]) == 20
    assert manifest["run_order"][:4] == [
        {"chapter": 1, "chapter_id": "e001-c1", "variant": "baseline"},
        {"chapter": 1, "chapter_id": "e001-c1", "variant": "candidate"},
        {"chapter": 2, "chapter_id": "e001-c2", "variant": "baseline"},
        {"chapter": 2, "chapter_id": "e001-c2", "variant": "candidate"},
    ]
    assert Path(manifest["branch_snapshots"]["baseline"]) != Path(manifest["branch_snapshots"]["candidate"])
    assert all(row["history_source"] == row["variant"] for row in plan)
    assert all(row["story_state_source"] == row["variant"] for row in plan)
    assert all(row["dynamic_at_execution"] is (row["chapter"] > 1) for row in plan)
    assert all(row["harness"] == "none" for row in plan if row["variant"] == "baseline")
    assert all(row["harness"] == service.PRODUCTION_HARNESS for row in plan if row["variant"] == "candidate")


def test_phase7d_dry_run_has_cost_signal_and_review_contracts(tmp_path):
    from app.services.phase7d_experiment_service import prepare_phase7d_experiment

    result = prepare_phase7d_experiment(
        tmp_path,
        provider="DeepSeek",
        model="deepseek-v4-flash",
        price_config=_price(),
    )
    preflight = result["cost_preflight"]
    assert preflight["calls"] == 20
    assert preflight["worst_case_basis"] == "all_input_cache_miss"
    assert preflight["worst_case_cost"] <= 5.0
    assert preflight["hard_cap"] == 20.0
    assert preflight["blocked"] is False

    review = json.loads(Path(result["artifacts"]["human_review"]).read_text(encoding="utf-8"))
    report = json.loads(Path(result["artifacts"]["report_template"]).read_text(encoding="utf-8"))
    assert review["review_chapters"] == [1, 5, 10]
    assert review["dimensions"] == [
        "continuity", "character_voice", "plot_progression", "emotion", "pacing",
        "style_adherence", "readability", "web_novel_pull", "overall_preference",
    ]
    assert review["long_form_preference"]["choices"] == ["A", "B"]
    assert report["decay_curve"]["chapters"] == [1, 3, 5, 7, 10]
    assert report["long_form_signals"] == {
        name: None for name in (
            "state_conflict_rate", "character_consistency_signals", "relationship_continuity",
            "knowledge_continuity", "foreshadow_progression", "chapter_change_density",
            "chapter_mode_repetition", "style_compliance",
        )
    }
    assert report["conclusion"]["allowed_claim"] == "在 10 章实验范围内，Candidate 是否显示出更好的长程稳定性。"
    assert report["conclusion"]["forbidden_claim"] == "已证明百万字能力。"


def test_phase7d_blocks_only_at_hard_cap(tmp_path):
    from app.services.phase7d_experiment_service import prepare_phase7d_experiment

    result = prepare_phase7d_experiment(
        tmp_path,
        provider="DeepSeek",
        model="deepseek-v4-flash",
        price_config=_price(cache_miss_input_per_m=1_000_000, output_per_m=1_000_000),
    )
    assert result["status"] == "blocked"
    assert result["cost_preflight"]["worst_case_cost"] > 20.0
    assert result["cost_preflight"]["blocked"] is True


def test_phase7d_fake_execution_propagates_only_variant_local_state(tmp_path):
    from app.services.phase7d_experiment_service import prepare_phase7d_experiment
    from app.services.phase7d_execution_service import execute_phase7d_experiment

    prepared = prepare_phase7d_experiment(
        tmp_path,
        provider="DeepSeek",
        model="deepseek-v4-flash",
        price_config=_price(),
    )
    seen = []

    async def fake_call(request):
        seen.append(request)
        label = "基线" if request["variant"] == "baseline" else "候选"
        return {
            "text": f"{label}第{request['chapter']}章正文。状态变化：{label}状态{request['chapter']}。下一目标：继续查明盐灯真相。",
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300, "prompt_cache_hit_tokens": 0},
        }

    result = asyncio.run(
        execute_phase7d_experiment(
            Path(prepared["artifacts"]["manifest"]).parent,
            call_model=fake_call,
        )
    )

    assert result["status"] == "COMPLETED_PENDING_HUMAN_REVIEW"
    assert result["engineering"] == {"calls": 20, "failures": 0, "retries": 0, "rewrites": 0, "qa": 0}
    assert len(seen) == 20
    baseline_two = next(row for row in seen if row["variant"] == "baseline" and row["chapter"] == 2)
    candidate_two = next(row for row in seen if row["variant"] == "candidate" and row["chapter"] == 2)
    assert "基线第1章正文" in baseline_two["branch_history"]
    assert "候选第1章正文" not in baseline_two["branch_history"]
    assert "候选第1章正文" in candidate_two["branch_history"]
    assert "基线第1章正文" not in candidate_two["branch_history"]
    assert baseline_two["compiled_context"] == ""
    assert candidate_two["compiled_context"]
    assert candidate_two["story_state_version"] > 1
    assert set(result["long_form_signals"]) == {
        "baseline", "candidate", "decay_curve"
    }
    assert [row["chapter"] for row in result["long_form_signals"]["decay_curve"]] == [1, 3, 5, 7, 10]
    assert Path(result["artifacts"]["report"]).is_file()
