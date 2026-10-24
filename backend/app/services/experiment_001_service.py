"""Zero-call preparation for the isolated Experiment 001 Harness A/B test."""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app import db
from app.dao import chapter_contracts_dao, story_states_dao
from app.services import project_context_service


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "experiment_fixtures" / "experiment_001" / "frozen_state"
FIXED_FIELDS = ("provider", "model", "temperature", "max_tokens", "thinking_mode", "reasoning_effort", "seed", "story", "characters", "world", "outline", "chapter_target")
CHAPTERS = 3
VARIANTS = ("baseline", "candidate")
MAX_CALLS = CHAPTERS * len(VARIANTS)
MAX_TOKENS_PER_CALL = 1600
TARGET_CNY = 5.0
HARD_CAP_CNY = 20.0
SINGLE_CALL_CAP_CNY = 2.0
SHARED_SYSTEM_PROMPT = "你是一位中文小说创作助手，请严格按用户的要求完成任务。"


class ExperimentPreparationError(ValueError):
    pass


def _load(name: str) -> Any:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _estimated_tokens(chars: int) -> dict[str, Any]:
    """Tokenizers vary by provider; this is deliberately an approximate audit value."""
    return {"value": max(1, (chars + 3) // 4), "method": "chars_div_4", "approximate": True}


def _harness_version_hash() -> str:
    root = Path(__file__).resolve().parent
    files = ("project_context_service.py", "chapter_contract_service.py", "commercial_pacing_service.py", "character_voice_service.py", "style_engine/profile_service.py")
    return _hash({name: _file_hash(root / name) for name in files})


def _runner_version_hash() -> str:
    return _hash({"service": _file_hash(Path(__file__)), "cli": _file_hash(Path(__file__).parents[2] / "scripts" / "prepare_experiment_001.py")})


@contextmanager
def _isolated_snapshot(root: Path) -> Iterator[Path]:
    """Temporarily point production DAO code at a disposable experiment DB."""
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="experiment-001-", dir=root))
    snapshot_db = temp_dir / "snapshot.db"
    old_path = db.DB_PATH
    try:
        db.DB_PATH = snapshot_db
        db.init_db()
        yield snapshot_db
    finally:
        db.DB_PATH = old_path


def _seed_snapshot() -> tuple[str, list[dict[str, Any]]]:
    story = _load("story.json")
    characters = _load("characters.json")
    world_rules = _load("world_rules.json")
    outline = _load("outline.json")
    state = _load("story_state.json")
    contracts = _load("chapter_contracts.json")
    foreshadows = _load("active_foreshadows.json")
    history = _load("recent_history.json")
    project_id = "experiment-001-fixture"
    nodes = []
    for chapter in outline["chapters"]:
        nodes.append({"id": chapter["id"], "type": "chapter", "title": chapter["title"], "summary": chapter["objective"], "goal": chapter["objective"], "conflict": chapter["conflict"], "stateChange": chapter["state_change"], "hook": chapter["hook"], "characters": chapter["characters"], "events": chapter["events"], "mustAvoid": chapter["must_avoid"], "pacing": chapter["pacing"], "order": chapter["order"]})
    now = "2026-08-24T00:00:00Z"
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects (id, name, genre, premise, outline, characters, world_rules, style_config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (project_id, story["title"], story["genre"], story["premise"], db.json_dump({"mainSummary": story["premise"], "nodes": nodes}), db.json_dump([{"id": item["id"], "name": item["name"], "role": item["role"], "surfaceMotivation": item["goal"], "deepDesire": item["current_state"], "speechStyle": item["speech_habits"]} for item in characters]), db.json_dump([{"id": item["id"], "name": item["id"], "description": item["rule"]} for item in world_rules]), db.json_dump(_load("style_profile.json")), now, now))
        for chapter in outline["chapters"]:
            prior = next((item for item in history if item["chapter_id"] == chapter["id"]), {})
            conn.execute("INSERT INTO chapters (id, project_id, title, chapter_order, content, word_count, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (chapter["id"], project_id, chapter["title"], chapter["order"], prior.get("content", ""), len(prior.get("content", "")), prior.get("summary", ""), now, now))
        for item in foreshadows:
            conn.execute("INSERT INTO foreshadows (id, project_id, name, description, plant_chapter_id, target_chapter_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (item["id"], project_id, item["name"], item["description"], item["plant_chapter_id"], item["target_chapter_id"], item["status"], now, now))
        conn.execute("INSERT INTO novel_facts (id, project_id, fact_type, content, chapter_id, created_at) VALUES (?, ?, ?, ?, ?, ?)", ("e001-memory", project_id, "completed", "Lin keeps the lantern sealed; opening it costs an unrelated shared memory.", "e001-c1", now))
    story_states_dao.create_story_state(project_id, state)
    for chapter_id, contract in contracts.items():
        chapter_contracts_dao.save_contract(project_id, chapter_id, contract)
    return project_id, outline["chapters"]


def _fixed_inputs(*, provider: str, model: str, temperature: float, max_tokens: int, thinking_mode: str, reasoning_effort: str, seed: int | str) -> dict[str, Any]:
    return {"provider": provider, "model": model, "temperature": temperature, "max_tokens": max_tokens, "thinking_mode": thinking_mode, "reasoning_effort": reasoning_effort, "seed": seed, "story": _load("story.json"), "characters": _load("characters.json"), "world": _load("world_rules.json"), "outline": _load("outline.json"), "chapter_target": {"words": [3000, 5000], "chapters": CHAPTERS}}


def _audit_inputs(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    differences = [key for key in FIXED_FIELDS if baseline.get(key) != candidate.get(key)]
    if differences:
        raise ExperimentPreparationError("non-harness fixed inputs differ: " + ", ".join(differences))
    if baseline["harness"] == candidate["harness"]:
        raise ExperimentPreparationError("harness must be the only changed variable")
    return {"fixed_fields": list(FIXED_FIELDS), "only_changed_variable": "harness", "blocked": False}


def _cost_preflight(price_config: dict[str, Any] | None, worst_case_input_tokens: int, expected_input_tokens: int, max_call_input_tokens: int) -> dict[str, Any]:
    base = {"calls": MAX_CALLS, "retries": 0, "rewrites": 0, "qa": 0, "worst_case_input_tokens": worst_case_input_tokens, "worst_case_output_tokens": MAX_CALLS * MAX_TOKENS_PER_CALL, "expected_input_tokens": expected_input_tokens, "expected_output_tokens": MAX_CALLS * MAX_TOKENS_PER_CALL, "currency": "CNY", "worst_case_basis": "cache_miss_input", "target_cost": TARGET_CNY, "hard_cap": HARD_CAP_CNY, "single_call_cap": SINGLE_CALL_CAP_CNY}
    required = {"currency", "price_source", "price_checked_at", "price_schedule", "cache_hit_input_per_m", "cache_miss_input_per_m", "output_per_m"}
    if not price_config or set(price_config) < required or price_config.get("currency") != "CNY":
        return {**base, "worst_case_cost": "unknown", "expected_cost": "unknown", "cost_per_chapter": "unknown", "cost_per_variant": "unknown", "max_single_call_cost": "unknown", "blocked": True, "reason": "CNY price configuration is incomplete or unverified"}
    try:
        input_per_m = float(price_config["cache_miss_input_per_m"])
        output_per_m = float(price_config["output_per_m"])
    except (TypeError, ValueError):
        return {**base, "worst_case_cost": "unknown", "expected_cost": "unknown", "cost_per_chapter": "unknown", "cost_per_variant": "unknown", "max_single_call_cost": "unknown", "blocked": True, "reason": "CNY price configuration is invalid"}
    if input_per_m < 0 or output_per_m < 0:
        return {**base, "worst_case_cost": "unknown", "expected_cost": "unknown", "cost_per_chapter": "unknown", "cost_per_variant": "unknown", "max_single_call_cost": "unknown", "blocked": True, "reason": "CNY prices cannot be negative"}
    output_tokens = MAX_CALLS * MAX_TOKENS_PER_CALL
    worst = worst_case_input_tokens / 1_000_000 * input_per_m + output_tokens / 1_000_000 * output_per_m
    expected = expected_input_tokens / 1_000_000 * input_per_m + output_tokens / 1_000_000 * output_per_m
    max_single = max_call_input_tokens / 1_000_000 * input_per_m + MAX_TOKENS_PER_CALL / 1_000_000 * output_per_m
    blocked = worst > TARGET_CNY or max_single > SINGLE_CALL_CAP_CNY
    reason = "within ¥5 target"
    if worst >= HARD_CAP_CNY:
        reason = "worst-case estimate reaches ¥20 hard cap"
    elif worst > TARGET_CNY:
        reason = "worst-case estimate exceeds ¥5 target"
    elif max_single > SINGLE_CALL_CAP_CNY:
        reason = "a predicted call exceeds ¥2"
    return {**base, "worst_case_output_tokens": output_tokens, "worst_case_cost": round(worst, 6), "expected_cost": round(expected, 6), "cost_per_chapter": round(expected / CHAPTERS, 6), "cost_per_variant": round(expected / len(VARIANTS), 6), "max_single_call_cost": round(max_single, 6), "blocked": blocked, "reason": reason}


def _baseline_prompt(story: dict[str, Any], characters: list[dict[str, Any]], world: list[dict[str, Any]], chapter: dict[str, Any]) -> tuple[str, str]:
    system = SHARED_SYSTEM_PROMPT
    user = json.dumps({"story": story, "characters": characters, "world_rules": world, "chapter_objective": chapter["objective"]}, ensure_ascii=False, sort_keys=True)
    return system, user


def prepare_experiment_001(output_root: Path, *, provider: str, model: str, temperature: float = 0.8, max_tokens: int = MAX_TOKENS_PER_CALL, price_config: dict[str, Any] | None = None, thinking_mode: str = "disabled", reasoning_effort: str = "none", seed: int | None = None, baseline_overrides: dict[str, Any] | None = None, finalize: bool = False) -> dict[str, Any]:
    """Prepare audited prompts and review forms without any provider import or call."""
    namespace = "experiment-001-prepared"
    artifacts = output_root / namespace
    artifacts.mkdir(parents=True, exist_ok=True)
    production_db = Path(db.DB_PATH)
    production_before = _file_hash(production_db)
    fixed = _fixed_inputs(provider=provider, model=model, temperature=temperature, max_tokens=max_tokens, thinking_mode=thinking_mode, reasoning_effort=reasoning_effort, seed=seed if seed is not None else "unsupported")
    baseline = {**fixed, "harness": "none"}
    baseline.update(baseline_overrides or {})
    candidate = {**fixed, "harness": "ProjectContextService + StoryState + ChapterContract + StyleProfile + CharacterVoice + CommercialPacing + Memory"}
    input_audit = _audit_inputs(baseline, candidate)
    requests: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    with _isolated_snapshot(artifacts) as snapshot_db:
        project_id, chapters = _seed_snapshot()
        snapshot_path = str(snapshot_db)
        for chapter in chapters:
            system, user = _baseline_prompt(fixed["story"], fixed["characters"], fixed["world"], chapter)
            base_tokens = _estimated_tokens(len(user))
            fixed_hashes = {"story_hash": _hash(fixed["story"]), "character_hash": _hash(fixed["characters"]), "world_hash": _hash(fixed["world"]), "outline_hash": _hash(fixed["outline"]), "chapter_target_hash": _hash(chapter)}
            requests["baseline"].append({"chapter_id": chapter["id"], "system_prompt": system, "user_prompt": user, "compiled_context": "", "user_prompt_chars": len(user), "estimated_input_tokens": base_tokens, "prompt_hash": _hash([system, user]), "context_hash": _hash(""), **fixed_hashes})
            context = project_context_service.assemble_project_context(project_id=project_id, chapter_id=chapter["id"], query=chapter["objective"], profile="scene_writing", include_current_text=False)
            candidate_system = SHARED_SYSTEM_PROMPT
            candidate_user = user + "\n\nNovelCraft compiled context:\n" + context["text"]
            requests["candidate"].append({"chapter_id": chapter["id"], "system_prompt": candidate_system, "user_prompt": candidate_user, "compiled_context": context["text"], "context_chars": len(context["text"]), "estimated_context_tokens": _estimated_tokens(len(context["text"])), "user_prompt_chars": len(candidate_user), "estimated_input_tokens": _estimated_tokens(len(candidate_user)), "context_blocks": context["blocks"], "prompt_hash": _hash([candidate_system, candidate_user]), "context_hash": _hash(context["text"]), **fixed_hashes})
    production_after = _file_hash(production_db)
    if production_before != production_after:
        raise ExperimentPreparationError("production database changed during experiment preparation")
    worst_input = max(row["estimated_input_tokens"]["value"] for rows in requests.values() for row in rows) * MAX_CALLS
    expected_input = sum(row["estimated_input_tokens"]["value"] for rows in requests.values() for row in rows)
    max_call_input = max(row["estimated_input_tokens"]["value"] for rows in requests.values() for row in rows)
    preflight = _cost_preflight(price_config, worst_input, expected_input, max_call_input)
    status = "prepared_do_not_run" if not preflight["blocked"] else "blocked"
    prompt_file = artifacts / "frozen_requests.json"
    frozen_text = json.dumps(requests, ensure_ascii=False, indent=2) + "\n"
    if prompt_file.exists() and prompt_file.read_text(encoding="utf-8") != frozen_text and not finalize:
        raise ExperimentPreparationError("frozen requests already exist; explicit finalize is required to replace them")
    prompt_file.write_text(frozen_text, encoding="utf-8")
    labels = ["A", "B"]
    random.Random(1001).shuffle(labels)
    mapping = {labels[0]: "baseline", labels[1]: "candidate"}
    # Evaluation material never discloses the mapping or provider/model.
    blind = {"experiment_id": "Experiment-001", "review_mode": "single-blind internal review", "variants": {label: [{"chapter_id": row["chapter_id"], "text": "PENDING_GENERATION"} for row in requests[name]] for label, name in mapping.items()}, "dimensions": ["continuity", "plot_progression", "character_voice", "emotion", "pacing", "style_adherence", "readability", "web_novel_pull", "overall_preference", "state_consistency", "memory_usefulness", "character_arc_coherence"], "reason": ""}
    blind_path = artifacts / "blind_review.json"
    blind_path.write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    mapping_path = artifacts / "private_blind_mapping.json"
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    context_audit = {"token_estimation": {"method": "chars_div_4", "approximate": True, "provider_usage": "not available until a real run"}, "chapters": [{"chapter_id": chapter["id"], "baseline_input_size": requests["baseline"][index]["user_prompt_chars"], "candidate_input_size": requests["candidate"][index]["user_prompt_chars"], "delta_input_size": requests["candidate"][index]["user_prompt_chars"] - requests["baseline"][index]["user_prompt_chars"], "candidate_context_blocks": [block["key"] for block in requests["candidate"][index]["context_blocks"]], "injected": {"story_state": "story_state" in [block["key"] for block in requests["candidate"][index]["context_blocks"]], "chapter_contract": "chapter_contract" in [block["key"] for block in requests["candidate"][index]["context_blocks"]], "style": "style" in [block["key"] for block in requests["candidate"][index]["context_blocks"]], "character_voice": "characters" in [block["key"] for block in requests["candidate"][index]["context_blocks"]], "pacing": "chapter_contract" in [block["key"] for block in requests["candidate"][index]["context_blocks"]], "memory": "facts" in [block["key"] for block in requests["candidate"][index]["context_blocks"]], "recent_history": "recent_summaries" in [block["key"] for block in requests["candidate"][index]["context_blocks"]]}} for index, chapter in enumerate(chapters)]}
    context_audit_path = artifacts / "experiment-001-context-audit.json"
    context_audit_path.write_text(json.dumps(context_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result_template = {"engineering": {"actual_calls": None, "input_tokens": None, "output_tokens": None, "total_tokens": None, "cost": None, "latency": None, "retry": 0, "rewrite": 0}, "deterministic_signals": {"state_conflicts": None, "state_changes": None, "repetition": None, "style_signals": None, "voice_signals": None}, "human": {name: None for name in ("continuity", "plot_progression", "character_voice", "emotion", "pacing", "style_adherence", "readability", "web_novel_pull", "overall_preference", "state_consistency", "memory_usefulness", "character_arc_coherence")}, "learning_actions": "disabled; results are report-only"}
    result_path = artifacts / "experiment-001-result.json"
    result_path.write_text(json.dumps(result_template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"experiment_id": "Experiment-001", "status": status, "real_llm_calls": 0, "experimental_namespace": namespace, "production_project_access": False, "production_database_hash_before": production_before, "production_database_hash_after": production_after, "fixed_parameters": {key: fixed[key] for key in ("provider", "model", "temperature", "max_tokens", "thinking_mode", "reasoning_effort", "seed")}, "price_config": price_config or {}, "run_order": [{"chapter_id": chapter["id"], "variant": variant} for chapter in chapters for variant in VARIANTS], "input_audit": input_audit, "cost_preflight": preflight, "blind_mapping_seed": 1001, "frozen_state_hash": _hash({name: _load(name) for name in sorted(path.name for path in FIXTURE_ROOT.iterdir())}), "harness_version_hash": _harness_version_hash(), "runner_version_hash": _runner_version_hash(), "requests": {name: [{key: row[key] for key in ("chapter_id", "prompt_hash", "context_hash", "story_hash", "character_hash", "world_hash", "outline_hash", "chapter_target_hash")} for row in rows] for name, rows in requests.items()}}
    manifest_path = artifacts / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": status, "real_llm_calls": 0, "experiment_namespace": namespace, "snapshot_db": snapshot_path, "input_audit": input_audit, "cost_preflight": preflight, "requests": requests, "artifacts": {"manifest": str(manifest_path), "frozen_requests": str(prompt_file), "context_audit": str(context_audit_path), "result_template": str(result_path), "blind_review": str(blind_path), "blind_mapping": str(mapping_path)}}


def experiment_001_readiness(output_root: Path, *, core_tests_pass: bool, packaging_pass: bool, harness_spy_pass: bool, default_suite_zero_api: bool) -> dict[str, Any]:
    """Read the frozen preparation artifacts and return the sole execution gate."""
    artifacts = output_root / "experiment-001-prepared"
    manifest_path = artifacts / "manifest.json"
    required = ("frozen_requests.json", "blind_review.json", "experiment-001-context-audit.json", "experiment-001-result.json")
    blockers: list[str] = []
    if not core_tests_pass:
        blockers.append("core_tests_failed")
    if not packaging_pass:
        blockers.append("packaging_failed")
    if not harness_spy_pass:
        blockers.append("production_harness_spy_failed")
    if not default_suite_zero_api:
        blockers.append("default_suite_not_zero_api")
    if not manifest_path.exists():
        blockers.append("manifest_missing")
        manifest: dict[str, Any] = {}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = [name for name in required if not (artifacts / name).is_file()]
    if missing:
        blockers.append("required_artifacts_missing")
    if manifest.get("production_database_hash_before") != manifest.get("production_database_hash_after"):
        blockers.append("production_isolation_failed")
    fixed_parameters = manifest.get("fixed_parameters") or {}
    if not all(key in fixed_parameters for key in ("provider", "model", "temperature", "max_tokens", "thinking_mode", "reasoning_effort", "seed")):
        blockers.append("fixed_parameters_missing")
    price_config = manifest.get("price_config") or {}
    if not price_config.get("price_source") or not price_config.get("price_checked_at") or price_config.get("currency") != "CNY":
        blockers.append("price_source_missing")
    if len(manifest.get("run_order") or []) != MAX_CALLS:
        blockers.append("run_order_missing")
    if (manifest.get("cost_preflight") or {}).get("blocked"):
        blockers.append("cost_gate_blocked")
    return {"status": "READY_TO_RUN" if not blockers else "BLOCKED", "blocking_reasons": blockers, "artifacts": str(artifacts), "real_llm_calls": 0}
