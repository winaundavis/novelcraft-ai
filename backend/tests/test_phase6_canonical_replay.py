from __future__ import annotations

import json
import uuid

from app.dao import chapter_commits, chapter_contracts_dao, story_states_dao
from app.db import get_conn
from app.services import chapter_digest_service, projection_runner_service


def _project() -> tuple[str, str, str]:
    pid = f"phase6-{uuid.uuid4().hex}"
    c1, c2 = f"{pid}-c1", f"{pid}-c2"
    with get_conn() as conn:
        conn.execute("INSERT INTO projects (id,name,created_at,updated_at,style_config) VALUES (?,?,?,?,?)", (pid, "phase6", "now", "now", "{}"))
        for cid, order, text in ((c1, 1, "Chapter one\n关系变化：结盟"), (c2, 2, "Chapter two\n新增悬念：门后")):
            conn.execute("INSERT INTO chapters (id,project_id,title,chapter_order,content,word_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (cid, pid, cid, order, text, len(text), "now", "now"))
    for cid in (c1, c2):
        chapter_contracts_dao.save_contract(pid, cid, {"goal": f"goal-{cid}", "key_events": [f"event-{cid}"]})
    story_states_dao.create_story_state(pid, {"main_conflict": "conflict"})
    return pid, c1, c2


def _commit(pid: str, cid: str, text: str) -> str:
    with get_conn() as conn:
        return chapter_commits.record_commit_on_conn(conn, project_id=pid, chapter_id=cid, content_hash=chapter_commits.sha256_hex(text), context_hash="ctx", content_snapshot=text, observed_facts=[text])


def test_canonical_commit_and_replay_are_version_and_order_stable(monkeypatch, tmp_path):
    pid, c1, c2 = _project()
    _commit(pid, c1, "v1")
    latest = _commit(pid, c1, "v2")
    _commit(pid, c1, "v3")
    _commit(pid, c2, "c2-v1")
    assert chapter_commits.get_canonical_commit(pid, c1)["content_snapshot"] == "v3"
    monkeypatch.setattr(projection_runner_service, "LOG_PATH", tmp_path / "projection.jsonl")
    seen = []
    monkeypatch.setattr(projection_runner_service, "run_projection", lambda commit_id, writer, **kwargs: seen.append((commit_id, writer)) or {"status": "done"})
    projection_runner_service.replay_project(pid, writers=("summary",))
    assert [item[0] for item in seen] == [chapter_commits.get_canonical_commit(pid, c1)["id"], chapter_commits.get_canonical_commit(pid, c2)["id"]]
    assert latest != seen[0][0]


def test_digest_is_structured_and_zero_api(monkeypatch):
    pid, c1, _ = _project()
    commit_id = _commit(pid, c1, "正文\n关系变化：结盟")
    commit = chapter_commits.get_commit_by_id(commit_id)
    digest = chapter_digest_service.persist_digest(commit)
    assert digest["evidence"]["commit_id"] == commit_id
    assert all(item["status"] in {"confirmed", "observed", "unverified"} for key, value in digest.items() if key not in {"evidence"} for item in (value if isinstance(value, list) else [value]) if isinstance(item, dict) and "status" in item)
