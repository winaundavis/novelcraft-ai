"""Recycle operations use disposable projects and never call an LLM."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db import get_conn
from main import app, _request_windows

OWNER = {"X-NovelCraft-Invite": "trash-owner"}
OTHER = {"X-NovelCraft-Invite": "trash-other"}


@pytest.fixture
def workspace(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "trash-owner:user-a,trash-other:user-b")
    _request_windows.clear()
    with TestClient(app) as client:
        pid = client.post("/api/projects/", headers=OWNER, json={
            "name": "回收测试", "genre": "xuanhuan", "targetWordCount": 10000, "stylePreset": "epic",
        }).json()["data"]["id"]
        ids = [f"{pid}-c{i}" for i in range(1, 4)]
        outline = {"mainSummary": "测试", "nodes": [
            {"id": "v1", "type": "volume", "title": "第一卷", "order": 1},
            {"id": "a1", "type": "arc", "title": "小故事", "parentId": "v1", "order": 1},
            *[{"id": cid, "type": "chapter", "title": f"第{i}章", "parentId": "v1", "arcId": "a1", "order": i}
              for i, cid in enumerate(ids, 1)],
        ]}
        assert client.patch(f"/api/projects/{pid}", headers=OWNER, json={"outline": outline}).status_code == 200
        for i, cid in enumerate(ids[:2], 1):
            assert client.put(f"/api/projects/{pid}/chapters/{cid}", headers=OWNER, json={
                "title": f"第{i}章", "content": f"保存正文{i}。\n人物站在窗前。", "volumeId": "v1", "order": i,
            }).status_code == 200
        yield client, pid, ids, outline


def _bin(client, pid):
    response = client.get(f"/api/projects/{pid}/chapter-trash", headers=OWNER)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _delete(client, pid, cid):
    response = client.delete(f"/api/projects/{pid}/chapters/{cid}", headers=OWNER)
    assert response.status_code == 200, response.text
    return _bin(client, pid)["records"][0]


def _restore(client, pid, record, target, **extra):
    view = _bin(client, pid)
    item = next(t for t in view["targets"] if t["chapterId"] == target)
    return client.post(f"/api/projects/{pid}/chapter-trash/{record['id']}/restore", headers=OWNER, json={
        "targetChapterId": target, "outlineHash": view["outlineHash"],
        "targetContentHash": item["contentHash"], **extra,
    })


def test_delete_preserves_content_and_restore_to_selected_chapter(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    assert record["sourceChapterId"] == ids[0]
    assert "第一卷" in record["originalLocation"]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).status_code == 404
    detail = client.get(f"/api/projects/{pid}/chapter-trash/{record['id']}", headers=OWNER).json()["data"]
    assert detail["content"] == "保存正文1。\n人物站在窗前。"
    restored = _restore(client, pid, record, ids[2])
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["chapterId"] == ids[2]
    assert all(item["status"] == "done" for item in restored.json()["data"]["projections"]), restored.text
    chapter = client.get(f"/api/projects/{pid}/chapters/{ids[2]}", headers=OWNER).json()["data"]
    assert chapter["content"] == detail["content"]
    assert chapter["title"] == "第3章"
    commit = client.get(f"/api/projects/{pid}/chapters/{ids[2]}/canonical-commit", headers=OWNER).json()["data"]
    assert commit["content_snapshot"] == chapter["content"]
    assert commit["created_by"] == "author_restore"
    assert not _bin(client, pid)["records"]
    assert _restore(client, pid, record, ids[2]).status_code == 404


def test_recycle_archives_contracts_and_restore_keeps_foreign_keys_valid(workspace):
    import json
    from app.dao import chapter_contracts_dao

    client, pid, ids, _ = workspace
    first = chapter_contracts_dao.save_contract(pid, ids[0], {"goal": "synthetic first goal"})
    second = chapter_contracts_dao.save_contract(pid, ids[0], {"goal": "synthetic revised goal"})
    surviving = chapter_contracts_dao.save_contract(pid, ids[1], {"goal": "synthetic surviving goal"})
    record = _delete(client, pid, ids[0])

    assert chapter_contracts_dao.get_latest_contract(pid, ids[0]) is None
    assert chapter_contracts_dao.get_latest_contract(pid, ids[1]) == surviving
    with get_conn() as conn:
        derived = json.loads(conn.execute("SELECT derived_json FROM chapter_trash WHERE id=?", (record["id"],)).fetchone()[0])
        assert {row["id"] for row in derived["chapter_contracts"]} == {first["id"], second["id"]}
        assert conn.execute("PRAGMA foreign_key_check(chapter_contracts)").fetchall() == []

    restored = _restore(client, pid, record, ids[2])
    assert restored.status_code == 200, restored.text
    restored_contract = chapter_contracts_dao.get_latest_contract(pid, ids[2])
    assert restored_contract["id"] not in {first["id"], second["id"]}
    assert restored_contract["goal"] != second["goal"]
    assert chapter_contracts_dao.get_latest_contract(pid, ids[1]) == surviving
    with get_conn() as conn:
        assert conn.execute("PRAGMA foreign_key_check(chapter_contracts)").fetchall() == []


def test_replacement_requires_confirmation_and_keeps_displaced_body(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    assert _restore(client, pid, record, ids[1]).status_code == 409
    restored = _restore(client, pid, record, ids[1], confirmReplace=True)
    assert restored.status_code == 200, restored.text
    displaced = _bin(client, pid)["records"]
    assert len(displaced) == 1
    assert displaced[0]["id"] == restored.json()["data"]["displacedTrashId"]
    detail = client.get(f"/api/projects/{pid}/chapter-trash/{displaced[0]['id']}", headers=OWNER).json()["data"]
    assert detail["content"].startswith("保存正文2")


def test_stale_preview_never_overwrites_new_content(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    view = _bin(client, pid)
    target = next(t for t in view["targets"] if t["chapterId"] == ids[1])
    client.put(f"/api/projects/{pid}/chapters/{ids[1]}", headers=OWNER, json={"content": "另一个页面刚保存"})
    response = client.post(f"/api/projects/{pid}/chapter-trash/{record['id']}/restore", headers=OWNER, json={
        "targetChapterId": ids[1], "outlineHash": view["outlineHash"],
        "targetContentHash": target["contentHash"], "confirmReplace": True,
    })
    assert response.status_code == 409
    assert len(_bin(client, pid)["records"]) == 1


def test_outline_patch_archives_only_removed_chapters(workspace):
    client, pid, ids, outline = workspace
    updated = deepcopy(outline)
    updated["nodes"] = [node for node in updated["nodes"] if node["id"] != ids[0]]
    response = client.patch(f"/api/projects/{pid}", headers=OWNER, json={"outline": updated})
    assert response.status_code == 200, response.text
    assert [r["sourceChapterId"] for r in _bin(client, pid)["records"]] == [ids[0]]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[1]}", headers=OWNER).status_code == 200


def test_ownership_and_permanent_delete_are_scoped(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    path = f"/api/projects/{pid}/chapter-trash/{record['id']}"
    for headers, expected in (({}, 401), (OTHER, 403)):
        for method, url in (("get", path), ("delete", path), ("get", f"/api/projects/{pid}/chapter-trash")):
            assert getattr(client, method)(url, headers=headers).status_code == expected
        assert client.post(path + "/restore", headers=headers, json={"targetChapterId": ids[2], "outlineHash": "", "targetContentHash": ""}).status_code == expected
    assert client.delete(path, headers=OWNER).status_code == 200
    assert not _bin(client, pid)["records"]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[1]}", headers=OWNER).status_code == 200


def test_restore_rejects_non_outline_target(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    response = client.post(f"/api/projects/{pid}/chapter-trash/{record['id']}/restore", headers=OWNER, json={
        "targetChapterId": "foreign-chapter", "outlineHash": _bin(client, pid)["outlineHash"], "targetContentHash": "",
    })
    assert response.status_code == 404
    assert len(_bin(client, pid)["records"]) == 1


def test_archive_rolls_back_if_cleanup_fails(workspace, monkeypatch):
    from app.services import chapter_trash_service as trash
    client, pid, ids, _ = workspace
    def fail(*args, **kwargs):
        raise RuntimeError("simulated cleanup failure")
    monkeypatch.setattr(trash, "invalidate_chapter_on_conn", fail)
    with pytest.raises(RuntimeError):
        trash.recycle_chapter(pid, ids[0])
    assert not _bin(client, pid)["records"]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).status_code == 200


def test_outline_changed_and_cross_project_ids_cannot_be_restored(workspace):
    client, pid, ids, outline = workspace
    record = _delete(client, pid, ids[0])
    view = _bin(client, pid)
    updated = deepcopy(outline)
    updated["mainSummary"] = "新大纲"
    assert client.patch(f"/api/projects/{pid}", headers=OWNER, json={"outline": updated}).status_code == 200
    path = f"/api/projects/{pid}/chapter-trash/{record['id']}/restore"
    assert client.post(path, headers=OWNER, json={"targetChapterId": ids[2], "outlineHash": view["outlineHash"], "targetContentHash": ""}).status_code == 409
    another = client.post("/api/projects/", headers=OWNER, json={"name": "另一个项目", "genre": "xuanhuan", "targetWordCount": 10000, "stylePreset": "epic"}).json()["data"]["id"]
    assert client.get(f"/api/projects/{another}/chapter-trash/{record['id']}", headers=OWNER).status_code == 404
    assert client.delete(f"/api/projects/{another}/chapter-trash/{record['id']}", headers=OWNER).status_code == 404
    # Client-controlled outline IDs cannot capture existing chapters of another project.
    client.put(f"/api/projects/{another}/chapters/foreign-target", headers=OWNER, json={"content": "别的项目正文"})
    updated["nodes"][-1]["id"] = "foreign-target"
    client.patch(f"/api/projects/{pid}", headers=OWNER, json={"outline": updated})
    assert _restore(client, pid, record, "foreign-target").status_code == 404


def test_active_generation_prevents_recycle_and_restore(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    with get_conn() as conn:
        conn.execute("""INSERT INTO novel_jobs(id, project_id, mode, status, started_at, updated_at, created_at)
                        VALUES (?, ?, 'chapter', 'running', '', '', '')""", (pid + '-job', pid))
    try:
        assert client.delete(f"/api/projects/{pid}/chapters/{ids[1]}", headers=OWNER).status_code == 409
        assert _restore(client, pid, record, ids[2]).status_code == 409
        assert len(_bin(client, pid)["records"]) == 1
    finally:
        with get_conn() as conn:
            conn.execute("UPDATE novel_jobs SET status = 'cancelled' WHERE project_id = ?", (pid,))


def test_restore_commit_failure_rolls_back_displacement(workspace, monkeypatch):
    from app.services import chapter_trash_service as trash
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    view = _bin(client, pid)
    target = next(t for t in view["targets"] if t["chapterId"] == ids[1])
    def fail(*args, **kwargs):
        raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(trash.chapter_commits, "record_commit_on_conn", fail)
    with pytest.raises(RuntimeError):
        trash.restore_chapter(pid, record['id'], target_chapter_id=ids[1],
                              expected_outline_hash=view['outlineHash'], target_content_hash=target['contentHash'], confirm_replace=True)
    assert len(_bin(client, pid)["records"]) == 1
    body = client.get(f"/api/projects/{pid}/chapters/{ids[1]}", headers=OWNER).json()["data"]["content"]
    assert body.startswith("保存正文2")


def test_import_replacement_recycles_old_chapters(workspace):
    from app.api.imports import _upsert_import_project
    client, pid, ids, _ = workspace
    _upsert_import_project(project_id=pid, project_name="重新导入", owner_id="user-a", scan_result={
        "folderPath": "fixture", "totalWords": 0, "chapters": [], "analysis": {},
    })
    assert {r["sourceChapterId"] for r in _bin(client, pid)["records"]} == set(ids[:2])
    assert not client.get(f"/api/projects/{pid}/chapters/", headers=OWNER).json()["data"]


def test_recovery_failure_does_not_lose_restored_prose(workspace, monkeypatch):
    from app.services import projection_runner_service
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    def fail(*args, **kwargs):
        raise RuntimeError("simulated projection crash")
    monkeypatch.setattr(projection_runner_service, "run_projection", fail)
    response = _restore(client, pid, record, ids[2])
    assert response.status_code == 200
    assert response.json()["data"]["projections"][0]["status"] == "failed"
    assert client.get(f"/api/projects/{pid}/chapters/{ids[2]}", headers=OWNER).json()["data"]["content"].startswith("保存正文1")
    debt = client.get(f"/api/projects/{pid}/chapters/{ids[2]}/projections", headers=OWNER).json()["data"]
    assert any(p["status"] == "failed" for p in debt)


# ── 批量操作 ──────────────────────────────────────────────
#
# 批量还原只回「各自的原章节」，冲突一律跳过而不是覆盖；下面逐条钉住这些语义。

def _batch_restore(client, pid, record_ids, outline_hash=None):
    view = _bin(client, pid)
    return client.post(f"/api/projects/{pid}/chapter-trash/batch-restore", headers=OWNER, json={
        "ids": record_ids, "outlineHash": view["outlineHash"] if outline_hash is None else outline_hash,
    })


def _batch_delete(client, pid, record_ids):
    return client.post(f"/api/projects/{pid}/chapter-trash/batch-delete", headers=OWNER,
                       json={"ids": record_ids})


def _put_chapter(client, pid, cid, content):
    return client.put(f"/api/projects/{pid}/chapters/{cid}", headers=OWNER, json={
        "title": "第1章", "content": content, "volumeId": "v1", "order": 1,
    })


def test_batch_restore_sends_every_record_back_to_its_own_chapter(workspace):
    client, pid, ids, _ = workspace
    first = _delete(client, pid, ids[0])
    second = _delete(client, pid, ids[1])
    assert {r["id"] for r in _bin(client, pid)["records"]} == {first["id"], second["id"]}

    response = _batch_restore(client, pid, [first["id"], second["id"]])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {item["chapterId"] for item in data["restored"]} == {ids[0], ids[1]}
    assert data["skipped"] == []
    # 各自回到原章节，正文与原标题都要对得上
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).json()["data"]["content"].startswith("保存正文1")
    assert client.get(f"/api/projects/{pid}/chapters/{ids[1]}", headers=OWNER).json()["data"]["content"].startswith("保存正文2")
    assert not _bin(client, pid)["records"]


def test_batch_restore_skips_chapters_that_already_have_content(workspace):
    """A 方案：绝不为了凑齐批量而覆盖已有正文。"""
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    # 原章节重新有了正文（作者手写或再次创作）
    assert _put_chapter(client, pid, ids[0], "作者后来写的新正文。").status_code == 200

    response = _batch_restore(client, pid, [record["id"]])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["restored"] == []
    assert [item["reason"] for item in data["skipped"]] == ["target_has_content"]
    assert "不会覆盖" in data["skipped"][0]["message"]
    # 关键：正文原封不动，回收记录也还在
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).json()["data"]["content"] == "作者后来写的新正文。"
    assert [r["id"] for r in _bin(client, pid)["records"]] == [record["id"]]


def test_batch_restore_skips_records_whose_source_chapter_left_the_outline(workspace):
    client, pid, ids, outline = workspace
    record = _delete(client, pid, ids[0])
    shortened = deepcopy(outline)
    shortened["nodes"] = [n for n in shortened["nodes"] if n["id"] != ids[0]]
    assert client.patch(f"/api/projects/{pid}", headers=OWNER, json={"outline": shortened}).status_code == 200

    response = _batch_restore(client, pid, [record["id"]])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["restored"] == []
    assert [item["reason"] for item in data["skipped"]] == ["source_not_in_outline"]
    assert "单独选择目标章节" in data["skipped"][0]["message"]
    assert [r["id"] for r in _bin(client, pid)["records"]] == [record["id"]]


def test_batch_restore_gives_one_chapter_to_only_one_record(workspace):
    """同一原章节被两条记录指向时，只还原第一条，避免第二条覆盖刚还原的正文。"""
    client, pid, ids, _ = workspace
    older = _delete(client, pid, ids[0])
    # 原章节重新出现并被再次删除 → 回收站里出现两条 sourceChapterId 相同的记录
    assert _put_chapter(client, pid, ids[0], "第二轮草稿。").status_code == 200
    newer = _delete(client, pid, ids[0])
    assert {older["sourceChapterId"], newer["sourceChapterId"]} == {ids[0]}

    response = _batch_restore(client, pid, [older["id"], newer["id"]])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["trashId"] for item in data["restored"]] == [older["id"]]
    assert [(item["trashId"], item["reason"]) for item in data["skipped"]] == [(newer["id"], "duplicate_source")]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).json()["data"]["content"].startswith("保存正文1")
    # 被跳过的那条仍在回收站，作者可以单独处理
    assert [r["id"] for r in _bin(client, pid)["records"]] == [newer["id"]]


def test_batch_restore_reports_unknown_ids_without_failing_the_batch(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    response = _batch_restore(client, pid, [record["id"], "no-such-record"])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["trashId"] for item in data["restored"]] == [record["id"]]
    assert [(item["trashId"], item["reason"]) for item in data["skipped"]] == [("no-such-record", "missing_record")]


def test_batch_restore_with_a_stale_outline_hash_changes_nothing(workspace):
    """大纲已变化时必须整批零改动，而不是做一半。"""
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    response = _batch_restore(client, pid, [record["id"]], outline_hash="stale-hash")
    assert response.status_code == 409
    assert [r["id"] for r in _bin(client, pid)["records"]] == [record["id"]]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).status_code == 404


def test_batch_restore_refuses_while_a_job_is_running(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    with get_conn() as conn:
        conn.execute("""INSERT INTO novel_jobs
            (id, project_id, mode, status, message, started_at, updated_at, created_at)
            VALUES ('job-running', ?, 'single', 'running', '', '', '', '')""", (pid,))
    response = _batch_restore(client, pid, [record["id"]])
    assert response.status_code == 409
    assert "创作结束" in response.json()["detail"]
    # 创作期间不得有任何改动：回收记录仍在，原章节也没被还原
    assert [r["id"] for r in _bin(client, pid)["records"]] == [record["id"]]
    assert client.get(f"/api/projects/{pid}/chapters/{ids[0]}", headers=OWNER).status_code == 404


def test_batch_delete_removes_every_selected_record_and_is_repeatable(workspace):
    client, pid, ids, _ = workspace
    first = _delete(client, pid, ids[0])
    second = _delete(client, pid, ids[1])
    response = _batch_delete(client, pid, [first["id"], second["id"]])
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]["deleted"]) == {first["id"], second["id"]}
    assert response.json()["data"]["missing"] == []
    assert not _bin(client, pid)["records"]
    # 重复提交（例如网络重试）应报告 missing，而不是报错
    again = _batch_delete(client, pid, [first["id"], second["id"]])
    assert again.status_code == 200, again.text
    assert again.json()["data"]["deleted"] == []
    assert set(again.json()["data"]["missing"]) == {first["id"], second["id"]}


def test_batch_endpoints_reject_an_empty_selection_and_foreign_projects(workspace):
    client, pid, ids, _ = workspace
    record = _delete(client, pid, ids[0])
    assert client.post(f"/api/projects/{pid}/chapter-trash/batch-delete", headers=OWNER,
                       json={"ids": []}).status_code == 422
    assert client.post(f"/api/projects/{pid}/chapter-trash/batch-restore", headers=OWNER,
                       json={"ids": [], "outlineHash": _bin(client, pid)["outlineHash"]}).status_code == 422
    # 别的 owner 不能对自己的项目之外执行批量操作
    assert client.post(f"/api/projects/{pid}/chapter-trash/batch-delete", headers=OTHER,
                       json={"ids": [record["id"]]}).status_code in {403, 404}
    assert [r["id"] for r in _bin(client, pid)["records"]] == [record["id"]]

