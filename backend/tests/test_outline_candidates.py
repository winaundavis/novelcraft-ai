from __future__ import annotations


def test_outline_candidate_round_trip_is_project_and_user_scoped(tmp_path, monkeypatch):
    from app import db as app_db

    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "candidate.db")
    app_db.init_db()

    from app.dao.outline_candidates import (
        create_outline_candidate,
        get_outline_candidate,
        list_outline_candidates,
    )

    candidate = create_outline_candidate(
        project_id="project-a",
        user_id="user-a",
        run_id="run-a",
        base_outline_hash="hash-a",
        base_outline_version=1,
        candidate_outline_json={"nodes": []},
        previous_outline_json=None,
        diff_json={"changed": []},
        instruction="拆分第一卷",
        status="proposed",
    )

    loaded = get_outline_candidate(candidate["id"])
    assert loaded is not None
    assert loaded["project_id"] == "project-a"
    assert loaded["candidate_outline_json"] == {"nodes": []}
    assert list_outline_candidates("project-a", "user-a")[0]["id"] == candidate["id"]
    assert list_outline_candidates("project-a", "user-b") == []
