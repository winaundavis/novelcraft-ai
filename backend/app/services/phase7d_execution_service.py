"""Strict branch-isolated executor and deterministic reporter for Phase 7-D."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from app import db
from app.dao import chapter_contracts_dao, chapters as chapters_dao
from app.dao import foreshadows as foreshadows_dao
from app.dao import story_state_transitions_dao
from app.services import chapter_change_service, content_signals_service
from app.services import project_context_service, story_state_service, story_state_transition_service
from app.services.experiment_001_execution_service import (
    _contains_chinese_prose,
    _contains_latin_characters,
    _cost,
    _deepseek_call,
    _hash,
)
from app.services.phase7d_experiment_service import DECAY_CHAPTERS, HARD_CAP_CNY, PRODUCTION_HARNESS


CallModel = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@contextmanager
def _branch_database(path: str) -> Iterator[None]:
    previous = db.DB_PATH
    try:
        db.DB_PATH = Path(path)
        yield
    finally:
        db.DB_PATH = previous


def _chapter(frozen: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    return next(row for row in frozen["outline"]["chapters"] if row["id"] == chapter_id)


def _branch_history(project_id: str, chapter_order: int) -> str:
    chapters = [
        row for row in chapters_dao.list_chapters_global_order(project_id)
        if int(row.get("order") or row.get("chapterOrder") or 0) < chapter_order and str(row.get("content") or "").strip()
    ]
    recent = chapters[-2:]
    return "\n\n".join(f"前章{row.get('order') or row.get('chapterOrder')}：\n{row['content']}" for row in recent)


def _common_prompt(frozen: dict[str, Any], chapter: dict[str, Any], history: str, state: dict[str, Any]) -> str:
    return json.dumps(
        {
            "story": frozen["story"],
            "characters": frozen["characters"],
            "world_rules": frozen["world"],
            "chapter_objective": chapter["objective"],
            "branch_previous_prose": history or "无，当前为第一章。",
            "branch_story_state": state,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _persist_chapter(project_id: str, chapter: dict[str, Any], text: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE chapters SET content = ?, word_count = ?, summary = ?, status = 'completed', updated_at = ? WHERE id = ? AND project_id = ?",
            (text, len(text), text[:300], "2026-08-25T00:00:00Z", chapter["id"], project_id),
        )
    story_state_transition_service.apply_chapter_transition(
        project_id,
        chapter["id"],
        summary=text[:300],
        content=text,
    )


def _character_signal(chapters: list[dict[str, Any]], contracts: list[dict[str, Any]]) -> dict[str, Any]:
    return content_signals_service.character_consistency_signal(chapters, contracts)


def _signals(snapshot: str, variant: str, limit: int | None = None) -> dict[str, Any]:
    project_id = f"phase7d-{variant}"
    with _branch_database(snapshot):
        chapters = [row for row in chapters_dao.list_chapters_global_order(project_id) if str(row.get("content") or "").strip()]
        if limit is not None:
            chapters = chapters[:limit]
        ids = {row["id"] for row in chapters}
        transitions = [row for row in story_state_transitions_dao.list_transitions(project_id, limit=100) if row["chapter_id"] in ids]
        order = {row["id"]: index for index, row in enumerate(chapters)}
        transitions.sort(key=lambda row: order.get(row["chapter_id"], 999))
        contracts = [row for row in chapter_contracts_dao.list_latest_contracts(project_id) if row["chapter_id"] in ids]
        contracts.sort(key=lambda row: order.get(row["chapter_id"], 999))
        foreshadows = foreshadows_dao.list_foreshadows(project_id)
        base = content_signals_service.build_long_form_signals(chapters, transitions, contracts, foreshadows)
        change = chapter_change_service.build_change_report(chapters, transitions, contracts)
        latin_free = sum(not _contains_latin_characters(row.get("content")) for row in chapters)
        return {
            "state_conflict_rate": base["state_conflict_rate"],
            "character_consistency_signals": _character_signal(chapters, contracts),
            "relationship_continuity": {"observed_change_rate": base["relationship_change_rate"]},
            "knowledge_continuity": {"observed_change_rate": base["knowledge_change_rate"]},
            "foreshadow_progression": base["foreshadow_progress_rate"],
            "chapter_change_density": change["metrics"]["chapter_change_rate"],
            "chapter_mode_repetition": base["chapter_mode_repeat_rate"],
            "style_compliance": {
                "profile_coverage": base["style_profile_coverage"],
                "latin_free_rate": round(latin_free / len(chapters), 4) if chapters else 0.0,
                "is_literary_judgment": False,
            },
            "repetition_evidence": change["repetition"],
            "chapters_measured": len(chapters),
        }


def _long_form_report(manifest: dict[str, Any]) -> dict[str, Any]:
    snapshots = manifest["branch_snapshots"]
    variants = {variant: _signals(snapshots[variant], variant) for variant in ("baseline", "candidate")}
    decay = []
    for chapter in DECAY_CHAPTERS:
        decay.append(
            {
                "chapter": chapter,
                "baseline": _signals(snapshots["baseline"], "baseline", chapter),
                "candidate": _signals(snapshots["candidate"], "candidate", chapter),
            }
        )
    return {**variants, "decay_curve": decay}


async def execute_phase7d_experiment(artifact_root: Path, *, call_model: CallModel | None = None) -> dict[str, Any]:
    manifest = json.loads((artifact_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "prepared_do_not_run" or manifest.get("cost_preflight", {}).get("blocked"):
        raise RuntimeError("Phase 7-D experiment is not ready")
    if manifest["call_plan"] != {"chapters": 10, "variants": 2, "primary_calls": 20, "retries": 0, "rewrites": 0, "qa": 0}:
        raise RuntimeError("Phase 7-D call plan is not frozen at 20 primary calls")
    fixed = manifest["fixed_parameters"]
    if fixed["provider"] != "DeepSeek" or fixed["model"] != "deepseek-v4-flash":
        raise RuntimeError("frozen provider/model is not DeepSeek V4 Flash")
    frozen = json.loads((artifact_root / "frozen_inputs.json").read_text(encoding="utf-8"))
    if manifest["frozen_input_hash"] != __import__("app.services.phase7d_experiment_service", fromlist=["_hash"])._hash(frozen):
        raise RuntimeError("frozen Phase 7-D inputs changed")

    output = artifact_root / "execution"
    for name in ("outputs", "audit", "results"):
        (output / name).mkdir(parents=True, exist_ok=True)
    production_before = _hash(Path(db.DB_PATH))
    caller = call_model or _deepseek_call
    calls: list[dict[str, Any]] = []
    failed_variants: set[str] = set()
    started_experiment = time.monotonic()

    for position, item in enumerate(manifest["run_order"], start=1):
        variant = item["variant"]
        if variant in failed_variants:
            calls.append({**item, "position": position, "status": "skipped_after_branch_failure"})
            continue
        chapter = _chapter(frozen, item["chapter_id"])
        snapshot = manifest["branch_snapshots"][variant]
        project_id = f"phase7d-{variant}"
        started = time.monotonic()
        try:
            with _branch_database(snapshot):
                history = _branch_history(project_id, chapter["order"])
                state = story_state_service.get_story_state(project_id)
                compiled_context = ""
                context_blocks: list[str] = []
                if variant == "candidate":
                    context = project_context_service.assemble_project_context(
                        project_id=project_id,
                        chapter_id=chapter["id"],
                        query=chapter["objective"],
                        profile="scene_writing",
                        include_current_text=False,
                    )
                    compiled_context = context["text"]
                    context_blocks = [row["key"] for row in context["blocks"]]
                user_prompt = _common_prompt(frozen, chapter, history, state)
                if compiled_context:
                    user_prompt += "\n\nNovelCraft compiled context:\n" + compiled_context
                request = {
                    **fixed,
                    "variant": variant,
                    "chapter": chapter["order"],
                    "chapter_id": chapter["id"],
                    "system_prompt": manifest.get("shared_system_prompt") or __import__("app.services.experiment_001_service", fromlist=["SHARED_SYSTEM_PROMPT"]).SHARED_SYSTEM_PROMPT,
                    "user_prompt": user_prompt,
                    "branch_history": history,
                    "story_state_version": int(state.get("version") or 1),
                    "compiled_context": compiled_context,
                    "context_blocks": context_blocks,
                    "harness": "none" if variant == "baseline" else PRODUCTION_HARNESS,
                }
                response = await caller(request)
                usage = response.get("usage") or {}
                text = response.get("text")
                if not _contains_chinese_prose(text):
                    raise RuntimeError("model response does not contain Chinese prose")
                if _contains_latin_characters(text):
                    raise RuntimeError("model response contains Latin characters")
                if not all(key in usage for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
                    raise RuntimeError("missing prose or usage audit")
                actual_cost, cache_status = _cost(usage, manifest["price_config"])
                if sum(float(row.get("actual_cost") or 0) for row in calls) + actual_cost > HARD_CAP_CNY:
                    raise RuntimeError("Phase 7-D hard cost cap would be exceeded")
                _persist_chapter(project_id, chapter, str(text))
            path = output / "outputs" / f"{position:02d}-{variant}-{chapter['id']}.txt"
            path.write_text(str(text), encoding="utf-8")
            calls.append({**item, "position": position, "status": "success", "elapsed_s": round(time.monotonic() - started, 3), "actual_input_tokens": int(usage["prompt_tokens"]), "actual_output_tokens": int(usage["completion_tokens"]), "actual_total_tokens": int(usage["total_tokens"]), "cache_hit": int(usage.get("prompt_cache_hit_tokens") or 0), "cache_status": cache_status, "actual_cost": actual_cost})
        except Exception as exc:
            calls.append({**item, "position": position, "status": "failed", "error": str(exc), "elapsed_s": round(time.monotonic() - started, 3)})
            failed_variants.add(variant)

    production_after = _hash(Path(db.DB_PATH))
    success = [row for row in calls if row["status"] == "success"]
    failures = [row for row in calls if row["status"] == "failed"]
    status = "COMPLETED_PENDING_HUMAN_REVIEW" if len(success) == 20 and not failures else "INCOMPLETE"
    if production_before != production_after:
        status = "FAILED_PRODUCTION_DB_MODIFIED"
    signals = _long_form_report(manifest)
    duration = round(time.monotonic() - started_experiment, 3)
    engineering = {"calls": len(success) + len(failures), "failures": len(failures), "retries": 0, "rewrites": 0, "qa": 0}
    tokens = {
        "baseline_input": sum(row.get("actual_input_tokens", 0) for row in success if row["variant"] == "baseline"),
        "candidate_input": sum(row.get("actual_input_tokens", 0) for row in success if row["variant"] == "candidate"),
        "baseline_output": sum(row.get("actual_output_tokens", 0) for row in success if row["variant"] == "baseline"),
        "candidate_output": sum(row.get("actual_output_tokens", 0) for row in success if row["variant"] == "candidate"),
        "cache_hit": sum(row.get("cache_hit", 0) for row in success),
    }
    baseline_cost = sum(row.get("actual_cost", 0) for row in success if row["variant"] == "baseline")
    candidate_cost = sum(row.get("actual_cost", 0) for row in success if row["variant"] == "candidate")
    report = {
        "experiment_id": "Experiment-001-Phase-7D",
        "status": status,
        "engineering": engineering,
        "duration": duration,
        "tokens": tokens,
        "cost": {"total": round(baseline_cost + candidate_cost, 6), "per_chapter": round((baseline_cost + candidate_cost) / 10, 6), "delta": round(candidate_cost - baseline_cost, 6)},
        "long_form_signals": signals,
        "human": {"chapters": [1, 5, 10], "reviews": None, "overall_preference": None},
        "conclusion": None,
        "calls": calls,
        "production_database_hash_before": production_before,
        "production_database_hash_after": production_after,
    }
    report_path = output / "results" / "phase7d-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "audit" / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["artifacts"] = {"report": str(report_path), "calls": str(output / "audit" / "calls.json")}
    return report
