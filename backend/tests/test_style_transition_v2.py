"""Regression contract for the v2 style-transition ledger.

These tests deliberately use no API or LLM.  They exercise only the persisted
plan and the on-connection completion hook so a chapter write and its style
ledger entry have the same transaction boundary.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from app.dao import projects as projects_dao
from app.dao import style_dao
from app.services.style_engine import transition_service
from test_style_engine import _DDL, _insert_chapter, _insert_project


@pytest.fixture
def style_db():
    """The legacy style fixture plus v2 profile fields needed for snapshots."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.executescript(
        """
        ALTER TABLE style_profiles ADD COLUMN style_kind TEXT NOT NULL DEFAULT 'reference';
        ALTER TABLE style_profiles ADD COLUMN identity TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE style_profiles ADD COLUMN mechanics TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE style_profiles ADD COLUMN boundaries TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE style_profiles ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE style_profiles ADD COLUMN parent_id TEXT NOT NULL DEFAULT '';
        """
    )
    conn.commit()

    def _get_conn():
        return conn

    with (
        patch.object(style_dao, "get_conn", _get_conn),
        patch.object(projects_dao, "get_conn", _get_conn),
        patch.object(transition_service, "get_conn", _get_conn),
    ):
        yield conn
    conn.close()


def _insert_profile(
    conn: sqlite3.Connection,
    project_id: str,
    profile_id: str,
    *,
    version: int,
    dimensions: dict,
    narrative_rule: str,
) -> None:
    now = "2026-09-28 00:00:00"
    conn.execute(
        """
        INSERT INTO style_profiles (
            id, project_id, name, source_type, status,
            narrative_rules, character_rules, language_rules, rhythm_rules,
            anti_ai_rules, metrics, dimensions, strengths, weaknesses,
            transition_plan, report, sample_count, sample_chars, llm_used,
            created_at, updated_at, style_kind, identity, mechanics,
            boundaries, version, parent_id
        ) VALUES (?, ?, ?, 'import', 'completed', ?, '[]', '[]', '[]',
                  '[]', '{}', ?, '[]', '[]', '{}', '', 0, 0, 0, ?, ?,
                  'reference', '{}', '{}', '{}', ?, '')
        """,
        (
            profile_id,
            project_id,
            profile_id,
            json.dumps([{"rule": narrative_rule}], ensure_ascii=False),
            json.dumps(dimensions, ensure_ascii=False),
            now,
            now,
            version,
        ),
    )
    conn.commit()


def _bind_profile(conn: sqlite3.Connection, project_id: str, profile_id: str) -> None:
    conn.execute(
        """INSERT INTO style_bindings
           (id, project_id, profile_id, scope, priority, weight, created_at, updated_at)
           VALUES (?, ?, ?, 'project', 1, 1.0, 'now', 'now')""",
        (f"binding-{project_id}", project_id, profile_id),
    )
    conn.commit()


def _plan(conn: sqlite3.Connection, project_id: str) -> dict:
    row = conn.execute(
        "SELECT plan FROM style_transitions WHERE project_id = ?", (project_id,)
    ).fetchone()
    assert row is not None
    return json.loads(row["plan"])


def _insert_chapter_on_conn(conn: sqlite3.Connection, project_id: str, chapter_id: str, content: str) -> None:
    conn.execute(
        """
        INSERT INTO chapters (
            id, project_id, title, chapter_order, content, word_count,
            status, summary, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'completed', '', '', 'now', 'now')
        """,
        (chapter_id, project_id, chapter_id, 99, content, len(content)),
    )


@pytest.mark.parametrize("pace,target", [("fast", 9), ("medium", 60), ("slow", 200)])
def test_start_transition_always_creates_v2_plan_with_pace_default(style_db, pace, target):
    _insert_project(style_db, "project", {"name": "Project preset"})

    transition = transition_service.start_transition("project", pace)

    assert transition["plan"]["schemaVersion"] == 2
    assert transition["targetChapters"] == target
    assert transition["completedCount"] == 0
    assert transition["revision"] == 1


@pytest.mark.parametrize("pace", [1, 17, 200])
def test_start_transition_accepts_integer_custom_target_count(style_db, pace):
    _insert_project(style_db, "project", {})

    transition = transition_service.start_transition("project", "custom", target_chapters=pace)

    assert transition["plan"]["schemaVersion"] == 2
    assert transition["targetChapters"] == pace


@pytest.mark.parametrize("target_chapters", [0, -1, 201, True, 1.5])
def test_start_transition_rejects_invalid_custom_target_counts(style_db, target_chapters):
    _insert_project(style_db, "project", {})

    with pytest.raises(ValueError):
        transition_service.start_transition("project", "custom", target_chapters=target_chapters)


def test_start_transition_rejects_unknown_pace_and_custom_without_count(style_db):
    _insert_project(style_db, "project", {})

    with pytest.raises(ValueError):
        transition_service.start_transition("project", "turbo")
    with pytest.raises(ValueError):
        transition_service.start_transition("project", "custom")


def test_start_transition_freezes_project_target_and_active_binding_source(style_db):
    project_style = {
        "presetKey": "project-preset",
        "name": "Project target",
        "avgSentenceLength": 22,
        "customPrompt": "Keep the project instruction",
    }
    _insert_project(style_db, "project", project_style)
    _insert_profile(
        style_db,
        "project",
        "active",
        version=7,
        dimensions={"avgSentenceLength": 9},
        narrative_rule="Reveal information through action",
    )
    _insert_profile(
        style_db,
        "project",
        "newer-but-inactive",
        version=99,
        dimensions={"avgSentenceLength": 99},
        narrative_rule="This inactive profile must not be selected",
    )
    _bind_profile(style_db, "project", "active")

    transition = transition_service.start_transition("project", "medium")
    snapshot = transition["plan"]

    assert snapshot["targetSnapshot"]["preset"] == {
        "presetKey": "project-preset",
        "name": "Project target",
        "avgSentenceLength": 22,
    }
    assert snapshot["targetSnapshot"]["presetKey"] == "project-preset"
    assert snapshot["targetSnapshot"]["customPrompt"] == "Keep the project instruction"
    assert snapshot["sourceSnapshot"]["profileId"] == "active"
    assert snapshot["sourceSnapshot"]["profileVersion"] == 7
    assert snapshot["sourceSnapshot"]["dimensions"] == {"avgSentenceLength": 9}
    assert snapshot["sourceSnapshot"]["rules"]["narrative_rules"] == [
        {"rule": "Reveal information through action"}
    ]

    # Subsequent project/profile edits cannot alter an already-issued plan.
    style_db.execute(
        "UPDATE projects SET style_config = ? WHERE id = ?",
        (json.dumps({"presetKey": "changed"}), "project"),
    )
    style_db.execute(
        "UPDATE style_profiles SET version = 8, dimensions = ? WHERE id = 'active'",
        (json.dumps({"avgSentenceLength": 12}),),
    )
    style_db.commit()
    assert _plan(style_db, "project")["targetSnapshot"]["preset"] == {
        "presetKey": "project-preset",
        "name": "Project target",
        "avgSentenceLength": 22,
    }
    assert _plan(style_db, "project")["sourceSnapshot"]["profileVersion"] == 7


def test_start_transition_explicit_target_resolves_and_freezes_default_preset(style_db):
    _insert_project(style_db, "project", {"presetKey": "project", "customPrompt": "old"})

    transition = transition_service.start_transition(
        "project",
        "fast",
        target_style={"presetKey": "emotional_resonance", "customPrompt": "Explicit instruction"},
    )

    target = transition["plan"]["targetSnapshot"]
    # `presetKey` resolves to the production default_style_config preset.
    assert target["preset"]["name"] == "Emotional Resonance"
    assert target["presetKey"] == "emotional_resonance"
    assert target["customPrompt"] == "Explicit instruction"


def test_progress_read_is_pure_for_v2_and_legacy_v1(style_db):
    _insert_project(style_db, "v2", {})
    v2 = transition_service.start_transition("v2", "fast")
    raw_v2_before = style_db.execute(
        "SELECT progress, plan, updated_at FROM style_transitions WHERE project_id = 'v2'"
    ).fetchone()
    observed_v2 = transition_service.get_transition_progress("v2")
    raw_v2_after = style_db.execute(
        "SELECT progress, plan, updated_at FROM style_transitions WHERE project_id = 'v2'"
    ).fetchone()
    assert observed_v2["planId"] == v2["planId"]
    assert tuple(raw_v2_before) == tuple(raw_v2_after)

    style_db.execute(
        """
        INSERT INTO style_transitions
            (id, project_id, profile_id, pace, progress, interval_chapters,
             total_steps, plan, started_at, updated_at)
        VALUES ('legacy', 'v1', '', 'medium', 0.0, 6, 10, ?, 'old', 'old')
        """,
        (json.dumps({"steps": [{"step": 1, "weight": 0.1}], "step": 0, "weight": 0.0}),),
    )
    style_db.commit()
    raw_v1_before = style_db.execute(
        "SELECT progress, plan, updated_at FROM style_transitions WHERE project_id = 'v1'"
    ).fetchone()
    transition_service.get_transition_progress("v1")
    raw_v1_after = style_db.execute(
        "SELECT progress, plan, updated_at FROM style_transitions WHERE project_id = 'v1'"
    ).fetchone()
    assert tuple(raw_v1_before) == tuple(raw_v1_after)
    assert "completedChapters" not in json.loads(raw_v1_after["plan"])


def test_completion_counts_only_nonempty_new_chapters_and_is_idempotent(style_db):
    _insert_project(style_db, "project", {})
    transition = transition_service.start_transition("project", "fast")
    plan_id = transition["planId"]

    with style_db:
        _insert_chapter_on_conn(style_db, "project", "empty", "")
        ignored = transition_service.record_chapter_completion_on_conn(
            style_db, "project", "empty", plan_id, 1
        )
    assert ignored["completedCount"] == 0
    assert ignored["revision"] == 1

    with style_db:
        _insert_chapter_on_conn(style_db, "project", "new", "body")
        first = transition_service.record_chapter_completion_on_conn(
            style_db, "project", "new", plan_id, 1, commit_id="commit-1"
        )
    with style_db:
        repeated = transition_service.record_chapter_completion_on_conn(
            style_db, "project", "new", plan_id, 1, commit_id="commit-2"
        )
    assert first["completedCount"] == repeated["completedCount"] == 1
    assert first["revision"] == repeated["revision"] == 2
    completed = _plan(style_db, "project")["completedChapters"]
    assert len(completed) == 1
    assert completed[0]["chapterId"] == "new"
    assert completed[0]["commitId"] == "commit-1"
    assert completed[0]["weight"] == pytest.approx(1 / 9, abs=0.001)


def test_baseline_and_excluded_chapters_never_count_but_wrong_plan_conflicts(style_db):
    _insert_project(style_db, "project", {})
    baseline_id = _insert_chapter(style_db, "project", 1, "pre-plan body")
    transition = transition_service.start_transition("project", "fast")

    with pytest.raises(transition_service.TransitionRevisionConflict):
        transition_service.record_chapter_completion_on_conn(
            style_db, "project", baseline_id, "wrong-plan", 1
        )

    with style_db:
        baseline = transition_service.record_chapter_completion_on_conn(
            style_db, "project", baseline_id, transition["planId"], 1
        )
        transition_service.exclude_chapters_on_conn(style_db, "project", ["imported", "restored"])
        _insert_chapter_on_conn(style_db, "project", "imported", "imported body")
        _insert_chapter_on_conn(style_db, "project", "restored", "restored body")
        excluded = transition_service.record_chapter_completion_on_conn(
            style_db, "project", "imported", transition["planId"], 1, source="import"
        )
        restored = transition_service.record_chapter_completion_on_conn(
            style_db, "project", "restored", transition["planId"], 1, source="restore"
        )
    assert baseline["completedCount"] == excluded["completedCount"] == restored["completedCount"] == 0
    assert _plan(style_db, "project")["excludedChapterIds"] == ["imported", "restored"]


def test_revision_conflicts_serialise_distinct_new_chapters_and_deletion_does_not_decrement(style_db):
    _insert_project(style_db, "project", {})
    transition = transition_service.start_transition("project", "fast")
    plan_id = transition["planId"]
    with style_db:
        _insert_chapter_on_conn(style_db, "project", "first", "first body")
        _insert_chapter_on_conn(style_db, "project", "second", "second body")
        accepted = transition_service.record_chapter_completion_on_conn(
            style_db, "project", "first", plan_id, 1
        )
        with pytest.raises(transition_service.TransitionRevisionConflict):
            transition_service.record_chapter_completion_on_conn(
                style_db, "project", "second", plan_id, 1
            )
    assert accepted["completedCount"] == 1
    style_db.execute("DELETE FROM chapters WHERE id = 'first'")
    style_db.commit()
    after_restart = transition_service.get_transition_progress("project")
    assert after_restart["completedCount"] == 1
    assert after_restart["progress"] == pytest.approx(1 / 9, abs=0.001)


def test_two_sqlite_connections_with_same_revision_allow_only_one_new_chapter(tmp_path):
    """Two writers may hold the same token, but only the first commit may claim it."""
    database = tmp_path / "transition.sqlite"
    bootstrap = sqlite3.connect(database)
    bootstrap.row_factory = sqlite3.Row
    bootstrap.executescript(_DDL)
    _insert_project(bootstrap, "project", {})
    bootstrap.close()

    def _get_conn():
        conn = sqlite3.connect(database, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn

    with (
        patch.object(style_dao, "get_conn", _get_conn),
        patch.object(projects_dao, "get_conn", _get_conn),
        patch.object(transition_service, "get_conn", _get_conn),
    ):
        transition = transition_service.start_transition("project", "fast")
        first = _get_conn()
        second = _get_conn()
        try:
            with first:
                _insert_chapter_on_conn(first, "project", "first", "first body")
            with second:
                _insert_chapter_on_conn(second, "project", "second", "second body")

            # Both writers generated from revision 1. The first transaction commits
            # before the second has a chance to use its now-stale token.
            with first:
                accepted = transition_service.record_chapter_completion_on_conn(
                    first, "project", "first", transition["planId"], 1
                )
            with pytest.raises(transition_service.TransitionRevisionConflict):
                with second:
                    transition_service.record_chapter_completion_on_conn(
                        second, "project", "second", transition["planId"], 1
                    )
            assert accepted["completedCount"] == 1
        finally:
            first.close()
            second.close()


def test_transaction_rolls_back_chapter_body_and_ledger_together_on_runtime_error(style_db):
    _insert_project(style_db, "project", {})
    transition = transition_service.start_transition("project", "fast")

    with pytest.raises(RuntimeError, match="post-write failure"):
        with style_db:
            _insert_chapter_on_conn(style_db, "project", "rolled-back", "body")
            transition_service.record_chapter_completion_on_conn(
                style_db, "project", "rolled-back", transition["planId"], 1
            )
            raise RuntimeError("post-write failure")

    assert style_db.execute(
        "SELECT 1 FROM chapters WHERE id = 'rolled-back'"
    ).fetchone() is None
    observed = transition_service.get_transition_progress("project")
    assert observed["completedCount"] == 0
    assert observed["revision"] == 1
