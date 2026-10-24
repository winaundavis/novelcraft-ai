from __future__ import annotations

import json
import uuid

import pytest

from app.dao import chapter_commits, chapter_contracts_dao, memory_entities, memory_fts, novel_facts, story_states_dao, story_state_transitions_dao
from app.db import get_conn
from app.services import projection_runner_service, project_context_service, novel_generation_service


MARKER = "回收专有标记"


@pytest.fixture
def seeded(monkeypatch, tmp_path):
    monkeypatch.setattr(projection_runner_service, "LOG_PATH", tmp_path / "projection.jsonl")
    pid = "trash-memory-" + uuid.uuid4().hex
    ids = [pid + "-1", pid + "-2"]
    with get_conn() as conn:
        conn.execute("INSERT INTO projects (id,name,created_at,updated_at,style_config) VALUES (?,?,?,?,?)", (pid, "test", "now", "now", "{}"))
        conn.execute('INSERT INTO volumes (id,project_id,title,"order",created_at,updated_at) VALUES (?,?,?,?,?,?)', (pid + "-vol", pid, "volume", 1, "now", "now"))
        for order, cid in enumerate(ids, 1):
            text = MARKER if order == 1 else "surviving content"
            conn.execute("INSERT INTO chapters (id,project_id,volume_id,title,chapter_order,content,word_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (cid, pid, pid + "-vol", "chapter", order, text, len(text), "now", "now"))
    for cid in ids:
        chapter_contracts_dao.save_contract(pid, cid, {"goal": "author goal"})
    story_states_dao.create_story_state(pid, {"main_conflict": "author conflict", "current_phase_goal": "author goal", "relationship_changes": ["author relation"], "current_mysteries": ["author mystery"], "unresolved_questions": ["author question"]})
    return pid, ids


def _commit(pid, cid, **evidence):
    with get_conn() as conn:
        text = conn.execute("SELECT content FROM chapters WHERE id=?", (cid,)).fetchone()["content"]
        return chapter_commits.record_commit_on_conn(conn, project_id=pid, chapter_id=cid, content_hash=chapter_commits.sha256_hex(text), content_snapshot=text, context_hash="", **evidence)


def _invalidate(pid, cid):
    from app.services.chapter_trash_memory import invalidate_chapter_on_conn
    with get_conn() as conn:
        snapshot = invalidate_chapter_on_conn(conn, pid, cid)
        conn.execute("DELETE FROM chapters WHERE id=? AND project_id=?", (cid, pid))
    return snapshot


def test_recycled_markers_leave_actual_generation_context_and_preserve_author_state(seeded):
    pid, (c1, c2) = seeded
    commit_id = _commit(pid, c1, observed_facts=[MARKER], relationship_changes=[MARKER], unresolved_questions=[MARKER], state_changes=[MARKER])
    assert all(projection_runner_service.run_projection(commit_id, writer)["status"] == "done" for writer in projection_runner_service.WRITERS)
    memory_entities.upsert_entity(pid, entity_type="character", name=MARKER, description=MARKER, source_chapter_id=c1)
    novel_facts.add_fact(pid, "state_changed", MARKER + "待确认", c1, status="uncertain")
    with get_conn() as conn:
        conn.execute("UPDATE volumes SET summary=?, summary_hash=? WHERE project_id=?", (MARKER, "old", pid))
    snapshot = _invalidate(pid, c1)
    assert MARKER in json.dumps(snapshot, ensure_ascii=False)
    state = story_states_dao.get_story_state(pid)
    assert MARKER not in json.dumps(state, ensure_ascii=False)
    assert state["main_conflict"] == "author conflict"
    assert state["relationship_changes"] == ["author relation"]
    assert state["current_mysteries"] == ["author mystery"]
    assert state["unresolved_questions"] == ["author question"]
    context = project_context_service.assemble_project_context(project_id=pid, chapter_id=c2)
    memory = novel_generation_service._build_memory_context(project_id=pid, chapter_nodes=[{"id": c2}], current_chapter_id=c2)
    assert MARKER not in json.dumps(context, ensure_ascii=False)
    assert MARKER not in json.dumps(memory, ensure_ascii=False)
    assert memory_fts.search_relevant(pid, [MARKER]) == []
    assert chapter_commits.get_canonical_commit(pid, c1) is None
    assert story_state_transitions_dao.get_transition(pid, c1) is None


def test_shared_contribution_survives_until_last_source_is_recycled(seeded):
    pid, (c1, c2) = seeded
    for cid in (c1, c2):
        commit_id = _commit(pid, cid, relationship_changes=[MARKER], unresolved_questions=[MARKER])
        assert projection_runner_service.run_projection(commit_id, "story_state")["status"] == "done"
    _invalidate(pid, c1)
    assert MARKER in story_states_dao.get_story_state(pid)["relationship_changes"]
    _invalidate(pid, c2)
    assert MARKER not in json.dumps(story_states_dao.get_story_state(pid), ensure_ascii=False)


def test_before_state_preserves_preexisting_author_duplicate(seeded):
    pid, (c1, _) = seeded
    story_states_dao.update_story_state(pid, {"relationship_changes": [MARKER]})
    commit_id = _commit(pid, c1, relationship_changes=[MARKER])
    projection_runner_service.run_projection(commit_id, "story_state")
    transition = story_state_transitions_dao.get_transition(pid, c1)
    assert transition.get("before_state", {}).get("relationship_changes") == [MARKER]
    _invalidate(pid, c1)
    assert story_states_dao.get_story_state(pid)["relationship_changes"] == [MARKER]


@pytest.mark.parametrize("reason", ["recycled", "superseded", "content_changed"])
def test_noncurrent_commit_cannot_project(seeded, reason):
    pid, (cid, _) = seeded
    commit_id = _commit(pid, cid, observed_facts=[MARKER])
    with get_conn() as conn:
        if reason == "content_changed":
            conn.execute("UPDATE chapters SET content='edited' WHERE id=?", (cid,))
        else:
            conn.execute("UPDATE chapter_commit_history SET status=? WHERE id=?", (reason, commit_id))
    with pytest.raises(ValueError, match="canonical|content"):
        projection_runner_service.run_projection(commit_id, "memory")
    assert novel_facts.list_completed_revealed_facts(pid) == []


def test_restored_fact_moves_both_source_ids_and_fts_excludes_inactive(seeded):
    pid, (c1, c2) = seeded
    novel_facts.add_fact(pid, "completed", MARKER, c1)
    novel_facts.supersede_facts_by_chapter(pid, c1)
    commit_id = _commit(pid, c2, observed_facts=[MARKER])
    assert projection_runner_service.run_projection(commit_id, "memory")["status"] == "done"
    fact = novel_facts.list_completed_revealed_facts(pid)[0]
    assert fact["chapter_id"] == fact["source_chapter_id"] == c2
    novel_facts.supersede_facts_by_chapter(pid, c2)
    assert memory_fts.sync_chapter_fts(pid, c2)
    assert c2 not in {row["chapter_id"] for row in memory_fts.search_relevant(pid, [MARKER])}


def test_invalidation_uses_callers_transaction(seeded):
    from app.services.chapter_trash_memory import invalidate_chapter_on_conn
    pid, (cid, _) = seeded
    commit_id = _commit(pid, cid, observed_facts=[MARKER], relationship_changes=[MARKER])
    projection_runner_service.run_projection(commit_id, "memory")
    projection_runner_service.run_projection(commit_id, "story_state")
    before = story_states_dao.get_story_state(pid)
    with pytest.raises(RuntimeError, match="rollback"):
        with get_conn() as conn:
            invalidate_chapter_on_conn(conn, pid, cid)
            raise RuntimeError("rollback")
    assert chapter_commits.get_canonical_commit(pid, cid)["id"] == commit_id
    assert novel_facts.list_completed_revealed_facts(pid)[0]["content"] == MARKER
    assert story_states_dao.get_story_state(pid) == before


def test_recycling_older_transition_does_not_reappear_from_later_before_state(seeded):
    pid, (c1, c2) = seeded
    first = _commit(pid, c1, state_changes=[MARKER])
    second = _commit(pid, c2, state_changes=["later state"])
    projection_runner_service.run_projection(first, "story_state")
    projection_runner_service.run_projection(second, "story_state")
    _invalidate(pid, c1)
    assert story_states_dao.get_story_state(pid)["last_major_state_change"] == "later state"
    _invalidate(pid, c2)
    assert MARKER not in json.dumps(story_states_dao.get_story_state(pid), ensure_ascii=False)


def test_conflicting_entity_from_recycled_chapter_does_not_remain_in_context(seeded):
    pid, (c1, c2) = seeded
    memory_entities.upsert_entity(pid, entity_type="character", name=MARKER, description=MARKER, status="conflict", source_chapter_id=c1)
    _invalidate(pid, c1)
    assert MARKER not in memory_entities.build_entity_state_block(pid)


def test_shared_fact_retains_surviving_canonical_source(seeded):
    pid, (c1, c2) = seeded
    for cid in (c1, c2):
        commit_id = _commit(pid, cid, observed_facts=[MARKER])
        projection_runner_service.run_projection(commit_id, "memory")
    _invalidate(pid, c1)
    facts = novel_facts.list_completed_revealed_facts(pid)
    assert len(facts) == 1 and facts[0]["content"] == MARKER
    assert facts[0]["chapter_id"] == facts[0]["source_chapter_id"] == c2
