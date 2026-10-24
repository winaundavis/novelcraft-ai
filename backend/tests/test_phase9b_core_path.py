"""Phase 9-B deterministic long-form chain evidence (no provider calls)."""

from unittest.mock import patch

from app.dao import chapter_commits
from app.services import projection_runner_service


def test_three_chapter_commit_projection_chain_preserves_order():
    commits = [
        {"id": "commit-1", "chapter_id": "chapter-1", "project_id": "project-1", "content_hash": "h1"},
        {"id": "commit-2", "chapter_id": "chapter-2", "project_id": "project-1", "content_hash": "h2"},
        {"id": "commit-3", "chapter_id": "chapter-3", "project_id": "project-1", "content_hash": "h3"},
    ]
    calls = []

    def fake_list(project_id):
        assert project_id == "project-1"
        return commits

    def fake_run(commit_id, writer, *, retry_of=""):
        calls.append((commit_id, writer, retry_of))
        return {"commit_id": commit_id, "writer": writer, "status": "done"}

    with patch.object(chapter_commits, "list_canonical_commits", side_effect=fake_list), \
         patch.object(projection_runner_service, "run_projection", side_effect=fake_run):
        result = projection_runner_service.replay_project("project-1")

    assert len(result) == 3 * len(projection_runner_service.WRITERS)
    assert [item[0] for item in calls[::len(projection_runner_service.WRITERS)]] == [
        "commit-1", "commit-2", "commit-3"
    ]
    assert all(item[2] == "replay" for item in calls)
    assert [item[1] for item in calls[:len(projection_runner_service.WRITERS)]] == list(
        projection_runner_service.WRITERS
    )
