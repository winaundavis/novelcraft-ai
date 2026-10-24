"""Zero-call preparation for the Phase 7-D ten-chapter continuity experiment."""

from __future__ import annotations

import hashlib
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app import db
from app.dao import chapter_contracts_dao, story_states_dao
from app.services import project_context_service
from app.services.experiment_001_service import SHARED_SYSTEM_PROMPT


COMMON_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "experiment_fixtures" / "experiment_001" / "frozen_state"
PHASE7D_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "experiment_fixtures" / "phase7d" / "frozen_state"
CHAPTERS = 10
VARIANTS = ("baseline", "candidate")
MAX_CALLS = CHAPTERS * len(VARIANTS)
MAX_TOKENS_PER_CALL = 1600
TARGET_CNY = 5.0
HARD_CAP_CNY = 20.0
DECAY_CHAPTERS = [1, 3, 5, 7, 10]
REVIEW_CHAPTERS = [1, 5, 10]
PRODUCTION_HARNESS = [
    "ProjectContextService",
    "Story State",
    "Chapter Contract",
    "Style Profile",
    "Character Voice",
    "Commercial Pacing",
    "Memory",
    "Recent History",
]
LONG_FORM_SIGNALS = (
    "state_conflict_rate",
    "character_consistency_signals",
    "relationship_continuity",
    "knowledge_continuity",
    "foreshadow_progression",
    "chapter_change_density",
    "chapter_mode_repetition",
    "style_compliance",
)
REVIEW_DIMENSIONS = (
    "continuity",
    "character_voice",
    "plot_progression",
    "emotion",
    "pacing",
    "style_adherence",
    "readability",
    "web_novel_pull",
    "overall_preference",
)


class Phase7DPreparationError(ValueError):
    pass


def _load_common(name: str) -> Any:
    return json.loads((COMMON_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _load_phase7d(name: str) -> Any:
    return json.loads((PHASE7D_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4)


@contextmanager
def _snapshot(root: Path, variant: str) -> Iterator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix=f"phase7d-{variant}-", dir=root)) / "snapshot.db"
    previous = db.DB_PATH
    try:
        db.DB_PATH = snapshot
        db.init_db()
        yield snapshot
    finally:
        db.DB_PATH = previous


def _frozen_inputs() -> dict[str, Any]:
    outline = _load_phase7d("outline.json")
    contracts = _load_phase7d("chapter_contracts.json")
    return {
        "story": _load_common("story.json"),
        "characters": _load_common("characters.json"),
        "world": _load_common("world_rules.json"),
        "outline": outline,
        "style": _load_common("style_profile.json"),
        "initial_story_state": _load_common("story_state.json"),
        "initial_memory": [
            {
                "id": "phase7d-memory-1",
                "type": "completed",
                "content": "Lin keeps the lantern sealed; opening it costs an unrelated shared memory.",
                "chapter_id": "e001-c1",
            }
        ],
        "initial_foreshadows": _load_common("active_foreshadows.json"),
        "chapter_contracts": contracts,
        "chapter_objectives": [
            {"chapter_id": row["id"], "objective": row["objective"]}
            for row in outline["chapters"]
        ],
    }


def _seed_branch(variant: str, frozen: dict[str, Any]) -> str:
    project_id = f"phase7d-{variant}"
    chapters = frozen["outline"]["chapters"]
    nodes = [
        {
            "id": row["id"],
            "type": "chapter",
            "title": row["title"],
            "summary": row["objective"],
            "goal": row["objective"],
            "conflict": row["conflict"],
            "stateChange": row["state_change"],
            "hook": row["hook"],
            "characters": row["characters"],
            "events": row["events"],
            "mustAvoid": row["must_avoid"],
            "pacing": row["pacing"],
            "chapterMode": row.get("chapter_mode", ""),
            "order": row["order"],
        }
        for row in chapters
    ]
    now = "2026-08-25T00:00:00Z"
    characters = frozen["characters"]
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, genre, premise, outline, characters, world_rules, style_config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                frozen["story"]["title"],
                frozen["story"]["genre"],
                frozen["story"]["premise"],
                db.json_dump({"mainSummary": frozen["story"]["premise"], "nodes": nodes}),
                db.json_dump([
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "role": item["role"],
                        "surfaceMotivation": item["goal"],
                        "deepDesire": item["current_state"],
                        "speechStyle": item["speech_habits"],
                    }
                    for item in characters
                ]),
                db.json_dump([{"id": item["id"], "name": item["id"], "description": item["rule"]} for item in frozen["world"]]),
                db.json_dump(frozen["style"]),
                now,
                now,
            ),
        )
        for chapter in chapters:
            conn.execute(
                "INSERT INTO chapters (id, project_id, title, chapter_order, content, word_count, summary, created_at, updated_at) VALUES (?, ?, ?, ?, '', 0, '', ?, ?)",
                (chapter["id"], project_id, chapter["title"], chapter["order"], now, now),
            )
        for item in frozen["initial_foreshadows"]:
            conn.execute(
                "INSERT INTO foreshadows (id, project_id, name, description, plant_chapter_id, target_chapter_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["id"], project_id, item["name"], item["description"], item["plant_chapter_id"], item["target_chapter_id"], item["status"], now, now),
            )
        for item in frozen["initial_memory"]:
            conn.execute(
                "INSERT INTO novel_facts (id, project_id, fact_type, content, chapter_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (item["id"], project_id, item["type"], item["content"], item["chapter_id"], now),
            )
    story_states_dao.create_story_state(project_id, frozen["initial_story_state"])
    for chapter_id, contract in frozen["chapter_contracts"].items():
        chapter_contracts_dao.save_contract(project_id, chapter_id, contract)
    return project_id


def _base_prompt(frozen: dict[str, Any], chapter: dict[str, Any]) -> str:
    return json.dumps(
        {
            "story": frozen["story"],
            "characters": frozen["characters"],
            "world_rules": frozen["world"],
            "chapter_objective": chapter["objective"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _cost_preflight(price: dict[str, Any] | None, expected_input: int, worst_input: int) -> dict[str, Any]:
    required = {
        "currency", "price_source", "price_checked_at", "price_schedule",
        "cache_hit_input_per_m", "cache_miss_input_per_m", "output_per_m",
    }
    base = {
        "calls": MAX_CALLS,
        "retries": 0,
        "rewrites": 0,
        "qa": 0,
        "expected_input_tokens": expected_input,
        "worst_case_input_tokens": worst_input,
        "expected_output_tokens": MAX_CALLS * MAX_TOKENS_PER_CALL,
        "worst_case_output_tokens": MAX_CALLS * MAX_TOKENS_PER_CALL,
        "worst_case_basis": "all_input_cache_miss",
        "target_cost": TARGET_CNY,
        "hard_cap": HARD_CAP_CNY,
        "currency": "CNY",
    }
    if not price or set(price) < required or price.get("currency") != "CNY":
        return {**base, "expected_cost": "unknown", "worst_case_cost": "unknown", "cost_per_chapter": "unknown", "blocked": True, "reason": "price configuration incomplete"}
    try:
        input_rate = float(price["cache_miss_input_per_m"])
        output_rate = float(price["output_per_m"])
    except (TypeError, ValueError):
        return {**base, "expected_cost": "unknown", "worst_case_cost": "unknown", "cost_per_chapter": "unknown", "blocked": True, "reason": "price configuration invalid"}
    output = MAX_CALLS * MAX_TOKENS_PER_CALL
    expected_cost = expected_input / 1_000_000 * input_rate + output / 1_000_000 * output_rate
    worst_cost = worst_input / 1_000_000 * input_rate + output / 1_000_000 * output_rate
    blocked = worst_cost > HARD_CAP_CNY
    return {
        **base,
        "expected_cost": round(expected_cost, 6),
        "worst_case_cost": round(worst_cost, 6),
        "cost_per_chapter": round(expected_cost / CHAPTERS, 6),
        "blocked": blocked,
        "reason": "exceeds ¥20 hard cap" if blocked else ("within ¥5 target" if worst_cost <= TARGET_CNY else "above ¥5 target but below hard cap"),
    }


def prepare_phase7d_experiment(
    output_root: Path,
    *,
    provider: str,
    model: str,
    price_config: dict[str, Any] | None,
    temperature: float = 0.8,
    max_tokens: int = MAX_TOKENS_PER_CALL,
    thinking_mode: str = "disabled",
    reasoning_effort: str = "none",
    seed: int | None = None,
    finalize: bool = False,
) -> dict[str, Any]:
    """Freeze Phase 7-D inputs and audit its dynamic execution plan without model calls."""
    artifacts = output_root / "phase7d-10chapter-prepared"
    artifacts.mkdir(parents=True, exist_ok=True)
    frozen = _frozen_inputs()
    production_db = Path(db.DB_PATH)
    production_before = _file_hash(production_db)
    snapshots: dict[str, str] = {}
    candidate_context_sizes: list[int] = []
    candidate_audit: list[dict[str, Any]] = []

    for variant in VARIANTS:
        with _snapshot(artifacts, variant) as snapshot:
            project_id = _seed_branch(variant, frozen)
            snapshots[variant] = str(snapshot.resolve())
            if variant == "candidate":
                for chapter in frozen["outline"]["chapters"]:
                    context = project_context_service.assemble_project_context(
                        project_id=project_id,
                        chapter_id=chapter["id"],
                        query=chapter["objective"],
                        profile="scene_writing",
                        include_current_text=False,
                    )
                    keys = [block["key"] for block in context["blocks"]]
                    candidate_context_sizes.append(len(context["text"]))
                    candidate_audit.append({"chapter": chapter["order"], "chapter_id": chapter["id"], "context_chars": len(context["text"]), "block_keys": keys})

    production_after = _file_hash(production_db)
    if production_before != production_after:
        raise Phase7DPreparationError("production database changed during dry-run")

    run_order = [
        {"chapter": chapter["order"], "chapter_id": chapter["id"], "variant": variant}
        for chapter in frozen["outline"]["chapters"]
        for variant in VARIANTS
    ]
    request_plan = [
        {
            **item,
            "system_prompt": SHARED_SYSTEM_PROMPT,
            "history_source": item["variant"],
            "story_state_source": item["variant"],
            "dynamic_at_execution": item["chapter"] > 1,
            "harness": "none" if item["variant"] == "baseline" else PRODUCTION_HARNESS,
            "retry": 0,
            "rewrite": 0,
            "qa": 0,
        }
        for item in run_order
    ]

    base_chars = [len(_base_prompt(frozen, chapter)) for chapter in frozen["outline"]["chapters"]]
    initial_expected = sum(_tokens(chars) for chars in base_chars) * 2 + sum(_tokens(chars) for chars in candidate_context_sizes)
    # Later prompts carry at most two branch-local chapters plus a bounded Story State and production context.
    history_chars = 2 * max_tokens * 4
    state_chars = 2400
    harness_chars = max(candidate_context_sizes or [0])
    worst_per_baseline = _tokens(max(base_chars) + history_chars + state_chars)
    worst_per_candidate = _tokens(max(base_chars) + history_chars + state_chars + harness_chars)
    worst_input = CHAPTERS * (worst_per_baseline + worst_per_candidate)
    preflight = _cost_preflight(price_config, initial_expected, worst_input)
    status = "blocked" if preflight["blocked"] else "prepared_do_not_run"

    frozen_path = artifacts / "frozen_inputs.json"
    frozen_text = json.dumps(frozen, ensure_ascii=False, indent=2) + "\n"
    if frozen_path.exists() and frozen_path.read_text(encoding="utf-8") != frozen_text and not finalize:
        raise Phase7DPreparationError("frozen inputs already exist; explicit finalize is required")
    frozen_path.write_text(frozen_text, encoding="utf-8")
    plan_path = artifacts / "request_plan.json"
    plan_path.write_text(json.dumps(request_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    human_review = {
        "review_mode": "single-blind long-form review",
        "review_chapters": REVIEW_CHAPTERS,
        "dimensions": list(REVIEW_DIMENSIONS),
        "variants": {"A": {str(chapter): None for chapter in REVIEW_CHAPTERS}, "B": {str(chapter): None for chapter in REVIEW_CHAPTERS}},
        "long_form_preference": {"question": "如果这是同一本小说，你更愿意继续读哪一个版本？", "choices": ["A", "B"], "answer": None, "reason": ""},
    }
    review_path = artifacts / "human_review.json"
    review_path.write_text(json.dumps(human_review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "engineering": {"calls": None, "failures": None, "retries": 0, "rewrites": 0, "duration": None},
        "tokens": {"baseline_input": None, "candidate_input": None, "baseline_output": None, "candidate_output": None, "cache_hit": None},
        "cost": {"total": None, "per_chapter": None, "delta": None},
        "long_form_signals": {name: None for name in LONG_FORM_SIGNALS},
        "decay_curve": {"chapters": DECAY_CHAPTERS, "baseline": None, "candidate": None, "delta": None},
        "human": {"chapters": REVIEW_CHAPTERS, "reviews": None, "overall_preference": None},
        "conclusion": {"allowed_claim": "在 10 章实验范围内，Candidate 是否显示出更好的长程稳定性。", "forbidden_claim": "已证明百万字能力。", "value": None},
    }
    report_path = artifacts / "report_template.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest = {
        "experiment_id": "Experiment-001-Phase-7D",
        "status": status,
        "real_llm_calls": 0,
        "fixed_parameters": {"provider": provider, "model": model, "temperature": temperature, "max_tokens": max_tokens, "thinking_mode": thinking_mode, "reasoning_effort": reasoning_effort, "seed": seed if seed is not None else "unsupported"},
        "call_plan": {"chapters": CHAPTERS, "variants": 2, "primary_calls": MAX_CALLS, "retries": 0, "rewrites": 0, "qa": 0},
        "run_order": run_order,
        "branch_snapshots": snapshots,
        "branch_isolation": {"history": "variant-local", "story_state": "variant-local", "cross_variant_reads": False},
        "production_harness": PRODUCTION_HARNESS,
        "candidate_context_audit": candidate_audit,
        "frozen_input_hash": _hash(frozen),
        "production_database_hash_before": production_before,
        "production_database_hash_after": production_after,
        "price_config": price_config or {},
        "cost_preflight": preflight,
        "automatic_expansion": False,
    }
    manifest_path = artifacts / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "status": status,
        "real_llm_calls": 0,
        "call_plan": manifest["call_plan"],
        "cost_preflight": preflight,
        "artifacts": {
            "manifest": str(manifest_path),
            "frozen_inputs": str(frozen_path),
            "request_plan": str(plan_path),
            "human_review": str(review_path),
            "report_template": str(report_path),
        },
    }
