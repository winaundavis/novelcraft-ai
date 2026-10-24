from __future__ import annotations

import copy
import uuid

import pytest

from app.dao import chapter_commits, chapter_contracts_dao, story_states_dao
from app.db import get_conn
from app.services import chapter_digest_service, doctor_service, projection_runner_service, readiness_service


def _seed() -> tuple[str, str]:
    project_id, chapter_id = f"closure-{uuid.uuid4().hex}", f"chapter-{uuid.uuid4().hex}"
    with get_conn() as conn:
        conn.execute("INSERT INTO projects (id, name, created_at, updated_at, style_config) VALUES (?, ?, ?, ?, ?)", (project_id, "closure", "now", "now", "{}"))
        conn.execute("INSERT INTO chapters (id, project_id, title, chapter_order, content, word_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (chapter_id, project_id, "chapter", 1, "关系变化：结盟\n新增悬念：门后", 12, "now", "now"))
    chapter_contracts_dao.save_contract(project_id, chapter_id, {"goal": "advance", "key_events": ["结盟"]})
    story_states_dao.create_story_state(project_id, {"main_conflict": "before"})
    with get_conn() as conn:
        commit_id = chapter_commits.record_commit_on_conn(
            conn, project_id=project_id, chapter_id=chapter_id,
            content_hash=chapter_commits.sha256_hex("关系变化：结盟\n新增悬念：门后"), context_hash="ctx",
            content_snapshot="关系变化：结盟\n新增悬念：门后",
            observed_facts=["结盟"], relationship_changes=["结盟"], unresolved_questions=["门后"], next_direction="调查门后",
        )
    return project_id, commit_id


def test_digest_items_include_field_level_commit_provenance():
    project_id, commit_id = _seed()
    digest = chapter_digest_service.persist_digest(chapter_commits.get_commit_by_id(commit_id))
    item = digest["relationship_changes"][0]
    assert item == {
        "value": "结盟", "source_type": "chapter_commit", "source_id": commit_id,
        "chapter_id": digest["evidence"]["chapter_id"], "evidence_level": "observed",
    }
    assert digest["evidence"]["canonical"] is False


def test_replay_is_idempotent_for_story_state_digest_and_memory(monkeypatch, tmp_path):
    project_id, _ = _seed()
    monkeypatch.setattr(projection_runner_service, "LOG_PATH", tmp_path / "projection.jsonl")
    first = projection_runner_service.replay_project(project_id)
    state_a = story_states_dao.get_story_state(project_id)
    commit = chapter_commits.list_canonical_commits(project_id)[0]
    digest_a = copy.deepcopy(chapter_digest_service.get_digest(project_id, commit["chapter_id"]))
    with get_conn() as conn:
        memory_a = [tuple(row) for row in conn.execute("SELECT fact_type, content, source_chapter_id FROM novel_facts WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()]
    second = projection_runner_service.replay_project(project_id)
    assert all(row["status"] == "done" for row in first + second)
    assert story_states_dao.get_story_state(project_id) == state_a
    assert chapter_digest_service.get_digest(project_id, commit["chapter_id"]) == digest_a
    with get_conn() as conn:
        memory_b = [tuple(row) for row in conn.execute("SELECT fact_type, content, source_chapter_id FROM novel_facts WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()]
    assert memory_b == memory_a


def test_doctor_reports_digest_bound_to_superseded_commit_with_action():
    project_id, old_id = _seed()
    commit = chapter_commits.get_commit_by_id(old_id)
    chapter_digest_service.persist_digest(commit)
    with get_conn() as conn:
        chapter_commits.record_commit_on_conn(conn, project_id=project_id, chapter_id=commit["chapter_id"], content_hash=commit["content_hash"], context_hash="new", content_snapshot=commit["content_snapshot"])
    report = doctor_service.inspect_project(project_id)
    issue = next(item for item in report["findings"] if item["area"] == "digest")
    assert issue["reason"] == "digest belongs to superseded commit"
    assert issue["action"] == "replay summary projection"


def test_doctor_ignores_legacy_projection_but_rejects_stale_canonical_projection():
    project_id, commit_id = _seed()
    commit = chapter_commits.get_commit_by_id(commit_id)
    with get_conn() as conn:
        chapter_commits.set_projection(project_id=project_id, chapter_id=commit["chapter_id"], projection_type="facts", status="failed", detail="legacy only")
        chapter_commits.set_projection(project_id=project_id, chapter_id=commit["chapter_id"], projection_type="memory", status="done", commit_id="superseded")
    report = doctor_service.inspect_project(project_id)
    assert not any(item.get("projection") == "facts" for item in report["findings"])
    stale = next(item for item in report["findings"] if item["area"] == "projection")
    assert stale["reason"] == "projection belongs to a non-canonical commit"


def test_doctor_flags_duplicate_canonical_commits_and_unbound_projection():
    project_id, first_id = _seed()
    first = chapter_commits.get_commit_by_id(first_id)
    with get_conn() as conn:
        second_id = chapter_commits.record_commit_on_conn(
            conn, project_id=project_id, chapter_id=first["chapter_id"],
            content_hash=first["content_hash"], context_hash="new", content_snapshot=first["content_snapshot"],
        )
        conn.execute("UPDATE chapter_commit_history SET status = 'accepted' WHERE id = ?", (first_id,))
    chapter_commits.set_projection(
        project_id=project_id, chapter_id=first["chapter_id"], projection_type="memory", status="done",
    )
    report = doctor_service.inspect_project(project_id)
    reasons = {item["reason"] for item in report["findings"]}
    assert "chapter has multiple canonical accepted commits" in reasons
    assert "projection has no canonical commit provenance" in reasons
    assert second_id


def test_experiment_readiness_blocks_unknown_cost_without_claiming_price():
    project_id, _ = _seed()
    readiness = readiness_service.experiment_readiness(project_id, core_tests_pass=True, default_suite_zero_api=True)
    assert readiness["status"] == "BLOCKED"
    assert readiness["cost_estimate"] == "unknown"
    assert "cost_plan_unknown" in readiness["blocking_reasons"]


@pytest.mark.asyncio
async def test_default_suite_network_guard_blocks_live_llm(monkeypatch, live_llm_call_spy):
    monkeypatch.delenv("RUN_LIVE_LLM", raising=False)
    import httpx
    from app.llm.provider import LLMProvider, LLMTransportError
    provider = LLMProvider(provider="openai", api_key="test", api_base="https://example.invalid")
    with pytest.raises(LLMTransportError) as raised:
        await provider._call_openai([{"role": "user", "content": "no network"}], "test", 0, 1)
    assert live_llm_call_spy() == 1
    assert isinstance(raised.value.__cause__, httpx.NetworkError)
    assert "disabled" in str(raised.value.__cause__)
    assert str(raised.value) == LLMTransportError.user_message
