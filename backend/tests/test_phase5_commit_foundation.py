from __future__ import annotations

import json
import uuid

from app.dao import chapter_commits, chapter_contracts_dao, story_states_dao
from app.db import get_conn
from app.services import doctor_service, projection_runner_service, write_gate_service
from app.services.experiment_runner_service import dry_run_experiment_001


def _seed(project_id: str = "", chapter_id: str = "", order: int = 1) -> tuple[str, str]:
    project_id = project_id or f"phase5-project-{uuid.uuid4().hex}"
    chapter_id = chapter_id or f"phase5-chapter-{uuid.uuid4().hex}"
    with get_conn() as conn:
        conn.execute("INSERT INTO projects (id, name, created_at, updated_at, style_config) VALUES (?, ?, ?, ?, ?)", (project_id, "Phase 5", "2026-08-20", "2026-08-20", "{}"))
        conn.execute("INSERT INTO chapters (id, project_id, title, chapter_order, content, word_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (chapter_id, project_id, "Chapter", order, "accepted chapter text", 19, "2026-08-20", "2026-08-20"))
    chapter_contracts_dao.save_contract(project_id, chapter_id, {"goal": "advance", "story_state_version": 1})
    story_states_dao.create_story_state(project_id, {"main_conflict": "before"})
    return project_id, chapter_id


def _commit(project_id: str, chapter_id: str, content: str = "accepted chapter text", **evidence: object) -> str:
    with get_conn() as conn:
        return chapter_commits.record_commit_on_conn(
            conn, project_id=project_id, chapter_id=chapter_id,
            content_hash=chapter_commits.sha256_hex(content), context_hash="ctx", content_snapshot=content,
            **evidence,
        )


def test_accepted_commit_is_immutable_versioned_and_detects_content_mismatch():
    project_id, chapter_id = _seed()
    first = _commit(project_id, chapter_id, observed_facts=["the gate opened"], contract_version=1, style_profile_version=2, story_state_version_before=1, created_by="author")
    second = _commit(project_id, chapter_id, content="revised accepted text")
    history = chapter_commits.get_commit_history(project_id, chapter_id)
    assert [row["version"] for row in history] == [1, 2]
    assert history[1]["parent_commit_id"] == first
    assert history[0]["observed_facts"] == ["the gate opened"]
    assert history[0]["content_snapshot"] == "accepted chapter text"
    assert chapter_commits.content_matches_commit(project_id, chapter_id) is False
    assert second != first


def test_projection_retry_and_log_are_deterministic_and_llm_free(monkeypatch, tmp_path):
    project_id, chapter_id = _seed()
    commit_id = _commit(project_id, chapter_id, observed_facts=["the gate opened"])
    monkeypatch.setattr(projection_runner_service, "LOG_PATH", tmp_path / "projection_log.jsonl")
    def boom(_: dict): raise RuntimeError("disk unavailable")
    monkeypatch.setitem(projection_runner_service._HANDLERS, "memory", boom)
    failed = projection_runner_service.run_projection(commit_id, "memory")
    assert failed["status"] == "failed"
    assert chapter_commits.get_commit(project_id, chapter_id)["id"] == commit_id
    monkeypatch.setitem(projection_runner_service._HANDLERS, "memory", projection_runner_service._apply_memory)
    retried = projection_runner_service.retry_failed_projection(project_id, chapter_id, "memory", retry_of=failed["run_id"])
    assert retried["status"] == "done"
    records = [json.loads(line) for line in (tmp_path / "projection_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all({"run_id", "chapter_id", "commit_id", "commit_hash", "writer", "status", "started_at", "finished_at", "error", "retry_of"} <= row.keys() for row in records)
    assert records[-1]["retry_of"] == failed["run_id"]


def test_replay_uses_accepted_commit_snapshots_in_order(monkeypatch, tmp_path):
    project_id, first_chapter = _seed()
    second_chapter = "phase5-chapter-2"
    with get_conn() as conn:
        conn.execute("INSERT INTO chapters (id, project_id, title, chapter_order, content, word_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (second_chapter, project_id, "Chapter 2", 2, "second accepted text", 20, "2026-08-20", "2026-08-20"))
    _commit(project_id, first_chapter, observed_facts=["first fact"])
    _commit(project_id, second_chapter, content="second accepted text", observed_facts=["second fact"])
    monkeypatch.setattr(projection_runner_service, "LOG_PATH", tmp_path / "projection_log.jsonl")
    result = projection_runner_service.replay_project(project_id, writers=("memory", "summary", "index"))
    assert len(result) == 6 and all(row["status"] == "done" for row in result)


def test_doctor_explains_commit_mismatch_and_write_gate_blocks_missing_contract():
    project_id, chapter_id = _seed()
    _commit(project_id, chapter_id, content="other accepted text")
    report = doctor_service.inspect_project(project_id)
    mismatch = next(item for item in report["findings"] if item["area"] == "commit")
    assert mismatch["level"] == "FAIL" and mismatch["suggestion"]
    blocked = write_gate_service.evaluate_precommit(project_id, "missing", "text", expected_version=1)
    assert not blocked["allowed"] and any(row["code"] == "contract_missing" for row in blocked["blockers"])


def test_experiment_dry_run_proves_only_harness_differs_and_prepares_blind_review():
    fixed = {"provider": "deepseek", "model": "deepseek-chat", "temperature": 0.7, "max_tokens": 2000, "story": "s", "characters": ["a"], "outline": "o", "target_length": 1000}
    report = dry_run_experiment_001({**fixed, "harness": "minimal"}, {**fixed, "harness": "novelcraft"}, seed=7)
    assert report["status"] == "prepared_do_not_run" and report["real_llm_calls"] == 0
    assert "overall_preference" in report["review_dimensions"]
