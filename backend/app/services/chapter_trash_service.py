"""Transactional, project-scoped chapter recycling. No provider calls."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.dao import chapter_commits, chapters, projects
from app.db import get_conn, json_dump
from app.models.schemas import gen_id
from app.services.chapter_trash_memory import invalidate_chapter_on_conn
from app.services.outline_candidate_service import outline_hash
from app.services.style_engine import transition_service


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _outline(conn, project_id: str) -> dict:
    row = conn.execute("SELECT outline FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "项目未找到。")
    return json.loads(row["outline"] or "null") or {"nodes": []}


def require_idle_on_conn(conn, project_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM novel_jobs WHERE project_id = ? AND status IN ('queued', 'running') LIMIT 1",
        (project_id,),
    ).fetchone():
        raise HTTPException(409, "正在创作，请等待创作结束后再调整或还原章节。")


def _location(outline: dict, chapter_id: str, fallback: str = "") -> str:
    nodes = {str(n.get("id")): n for n in outline.get("nodes", []) if isinstance(n, dict)}
    node = nodes.get(chapter_id, {})
    arc = nodes.get(node.get("arcId"), {})
    parent = nodes.get(node.get("parentId"), {})
    if parent.get("type") == "arc":
        arc, parent = parent, nodes.get(parent.get("parentId"), {})
    return " / ".join(str(n.get("title")) for n in (parent, arc, node) if n.get("title")) or fallback


def _record(row, *, include_content=False) -> dict:
    data = {
        "id": row["id"], "projectId": row["project_id"], "sourceChapterId": row["source_chapter_id"],
        "title": row["title"], "originalLocation": row["original_location"], "reason": row["reason"],
        "removedAt": row["removed_at"], "wordCount": row["word_count"], "contentHash": row["content_hash"],
    }
    if include_content:
        chapter = json.loads(row["chapter_json"])
        data.update(content=chapter.get("content", ""), notes=chapter.get("notes", ""))
    return data


def list_trash(project_id: str) -> dict:
    with get_conn() as conn:
        outline = _outline(conn, project_id)
        records = conn.execute("""SELECT id, project_id, source_chapter_id, title, original_location,
            reason, removed_at, word_count, content_hash FROM chapter_trash
            WHERE project_id = ? ORDER BY removed_at DESC, id""", (project_id,)).fetchall()
        active = {r["id"]: r for r in conn.execute("SELECT id, content FROM chapters WHERE project_id = ?", (project_id,))}
        targets = []
        for node in outline.get("nodes", []):
            if node.get("type") != "chapter":
                continue
            body = active[node["id"]]["content"] if node["id"] in active else ""
            targets.append({"chapterId": node["id"], "title": node.get("title") or "未命名章节",
                            "location": _location(outline, node["id"]), "hasContent": bool(body.strip()),
                            "contentHash": chapter_commits.sha256_hex(body)})
    return {"records": [_record(r) for r in records], "targets": targets, "outlineHash": outline_hash(outline)}


def get_trash(project_id: str, trash_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chapter_trash WHERE project_id = ? AND id = ?", (project_id, trash_id)).fetchone()
        if row is None:
            raise HTTPException(404, "回收记录未找到。")
        return _record(row, include_content=True)


def recycle_on_conn(conn, project_id: str, chapter_id: str, *, reason: str, outline: dict | None = None) -> str:
    row = conn.execute("SELECT * FROM chapters WHERE project_id = ? AND id = ?", (project_id, chapter_id)).fetchone()
    if row is None:
        raise HTTPException(404, "章节未找到。")
    outline = outline if outline is not None else _outline(conn, project_id)
    commit = conn.execute(
        "SELECT * FROM chapter_commit_history WHERE project_id = ? AND chapter_id = ? AND status = 'accepted' ORDER BY version DESC LIMIT 1",
        (project_id, chapter_id),
    ).fetchone()
    history_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chapter_commit_history WHERE project_id = ? AND chapter_id = ? AND status IN ('accepted', 'superseded')",
        (project_id, chapter_id),
    )]
    derived = invalidate_chapter_on_conn(conn, project_id, chapter_id)
    derived["commit_ids"] = history_ids
    record_id = gen_id()
    conn.execute(
        """INSERT INTO chapter_trash
           (id, project_id, source_chapter_id, title, original_location, reason, removed_at,
            word_count, content_hash, chapter_json, commit_json, derived_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (record_id, project_id, chapter_id, row["title"], _location(outline, chapter_id, row["title"]),
         reason, _now(), row["word_count"], chapter_commits.sha256_hex(row["content"]),
         json_dump(dict(row)), json_dump(dict(commit) if commit else {}), json_dump(derived)),
    )
    conn.execute("DELETE FROM chapters WHERE project_id = ? AND id = ?", (project_id, chapter_id))
    projects._update_project_word_count_on_conn(conn, project_id, _now())
    return record_id


def recycle_chapter(project_id: str, chapter_id: str) -> str:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        require_idle_on_conn(conn, project_id)
        return recycle_on_conn(conn, project_id, chapter_id, reason="manual_delete")


def reconcile_outline_on_conn(conn, project_id: str, old_outline: dict, new_outline: dict) -> list[str]:
    old_ids = {n["id"] for n in old_outline.get("nodes", []) if n.get("type") == "chapter"}
    new_ids = {n["id"] for n in new_outline.get("nodes", []) if n.get("type") == "chapter"}
    removed = old_ids - new_ids
    if not removed:
        return []
    require_idle_on_conn(conn, project_id)
    active = conn.execute("SELECT id FROM chapters WHERE project_id = ?", (project_id,)).fetchall()
    return [recycle_on_conn(conn, project_id, r["id"], reason="outline_removed", outline=old_outline)
            for r in active if r["id"] in removed]


def _restore_evidence(commit: dict, content: str) -> dict[str, Any]:
    if commit and commit.get("content_hash") == chapter_commits.sha256_hex(content):
        # Preserve observed evidence, never the old chapter contract or next-arc plan.
        return {key: json.loads(commit.get(key + "_json") or "[]") for key in (
            "observed_facts", "state_changes", "relationship_changes", "knowledge_changes", "conflict_changes",
            "foreshadow_changes", "payoffs", "unresolved_questions",
        )}
    from app.services.story_state_transition_service import extract_observed_state
    observed = extract_observed_state(content, {})
    return {"observed_facts": observed["knowledge"], "state_changes": observed["state_changes"],
            "relationship_changes": observed["relationships"], "knowledge_changes": observed["knowledge"],
            "conflict_changes": observed["conflicts"], "foreshadow_changes": observed["foreshadows"],
            "unresolved_questions": observed["mysteries"]}


def restore_chapter(project_id: str, trash_id: str, *, target_chapter_id: str,
                    expected_outline_hash: str, target_content_hash: str, confirm_replace: bool = False) -> dict:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        require_idle_on_conn(conn, project_id)
        row = conn.execute("SELECT * FROM chapter_trash WHERE project_id = ? AND id = ?", (project_id, trash_id)).fetchone()
        if row is None:
            raise HTTPException(404, "回收记录未找到，可能已还原或删除。")
        outline = _outline(conn, project_id)
        if outline_hash(outline) != expected_outline_hash:
            raise HTTPException(409, "大纲已变化，请刷新回收站后重新选择目标章节。")
        node = next((n for n in outline.get("nodes", []) if n.get("id") == target_chapter_id and n.get("type") == "chapter"), None)
        if node is None:
            raise HTTPException(404, "目标章节不在当前大纲中。")
        # IDs are globally unique: a forged outline must not acquire another project's chapter.
        target = conn.execute("SELECT * FROM chapters WHERE id = ?", (target_chapter_id,)).fetchone()
        if target is not None and target["project_id"] != project_id:
            raise HTTPException(404, "目标章节未找到。")
        body = target["content"] if target else ""
        if chapter_commits.sha256_hex(body) != target_content_hash:
            raise HTTPException(409, "目标正文已变化，请刷新回收站后重新确认。")
        if body.strip() and not confirm_replace:
            raise HTTPException(409, "目标章节已有正文。确认后旧正文将先移入回收站，再进行还原。")
        displaced = None
        if target:
            # Even an empty target may have notes/commit history; preserve its complete snapshot.
            displaced = recycle_on_conn(conn, project_id, target_chapter_id, reason="restore_replaced", outline=outline)
        source = json.loads(row["chapter_json"])
        parent_id = node.get("parentId") or ""
        parent = next((n for n in outline.get("nodes", []) if n.get("id") == parent_id), {})
        volume_id = parent.get("parentId", "") if parent.get("type") == "arc" else parent_id
        restored = {**source, "id": target_chapter_id, "project_id": project_id,
                    "volume_id": volume_id, "title": node.get("title") or source["title"],
                    "chapter_order": int(node.get("order") or 1), "summary": "", "updated_at": _now()}
        chapters._insert_chapter_on_conn(conn, restored)
        transition_service.exclude_chapters_on_conn(conn, project_id, [target_chapter_id, str(row["source_chapter_id"] or "")])
        commit_id = chapter_commits.record_commit_on_conn(
            conn, project_id=project_id, chapter_id=target_chapter_id,
            content_hash=chapter_commits.sha256_hex(source["content"]), content_snapshot=source["content"],
            context_hash=outline_hash(outline), created_by="author_restore",
            **_restore_evidence(json.loads(row["commit_json"]), source["content"]),
        )
        # Persist pending sync before releasing the transaction; a crash leaves visible debt.
        for writer in ("story_state", "memory", "summary", "index", "foreshadow"):
            conn.execute("""INSERT INTO memory_projections
                (id, project_id, chapter_id, projection_type, status, detail, commit_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'failed', '还原正文已保存，等待同步。', ?, ?, ?)
                ON CONFLICT(project_id, chapter_id, projection_type) DO UPDATE SET
                status = excluded.status, detail = excluded.detail, commit_id = excluded.commit_id, updated_at = excluded.updated_at""",
                (gen_id(), project_id, target_chapter_id, writer, commit_id, _now(), _now()))
        conn.execute("DELETE FROM chapter_trash WHERE project_id = ? AND id = ?", (project_id, trash_id))
        projects._update_project_word_count_on_conn(conn, project_id, _now())
    from app.services import projection_runner_service, story_state_service
    try:
        story_state_service.ensure_story_state(project_id)
        results = []
        for writer in projection_runner_service.WRITERS:
            result = projection_runner_service.run_projection(commit_id, writer)
            results.append({"writer": writer, "status": result["status"],
                            **({"error": "正文已还原，章节记忆待同步。"} if result["status"] != "done" else {})})
    except Exception:
        # Body and bin operations have already committed. A sync failure must not invite a second restore.
        results = [{"status": "failed", "error": "正文已还原，章节记忆待同步。"}]
    return {"chapterId": target_chapter_id, "displacedTrashId": displaced, "projections": results}


def permanently_delete(project_id: str, trash_id: str) -> None:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT derived_json FROM chapter_trash WHERE project_id = ? AND id = ?", (project_id, trash_id)).fetchone()
        if row is None:
            raise HTTPException(404, "回收记录未找到。")
        # Retain structural audit history but remove archived prose/evidence from its commits.
        for cid in json.loads(row["derived_json"]).get("commit_ids", []):
            conn.execute("""UPDATE chapter_commit_history SET content_snapshot = '',
                observed_facts_json = '[]', state_changes_json = '[]', relationship_changes_json = '[]',
                knowledge_changes_json = '[]', conflict_changes_json = '[]', foreshadow_changes_json = '[]',
                payoffs_json = '[]', unresolved_questions_json = '[]', next_direction = '', status = 'purged'
                WHERE project_id = ? AND id = ? AND status != 'accepted'""", (project_id, cid))
        conn.execute("DELETE FROM chapter_trash WHERE project_id = ? AND id = ?", (project_id, trash_id))


# ── 批量操作 ──────────────────────────────────────────────
#
# 两个批量接口都刻意复用上面的单条实现，而不是把事务体抽出来共用：
# 单条还原包含「位移旧正文 → 插入章节 → 记 commit → 排队并执行投影」一长串
# 精细逻辑，改动它的回归面远大于批量本身要解决的问题。
# 代价是每条各起一个事务，因此**失败/跳过都是逐条报告**，而不是整批回滚——
# 作者需要知道「哪几条没成功、为什么」，这比一个笼统的 500 更有用。
#
# 注意：这里不需要为「跳过」预留额外的同步债务。投影 runner 明确声明
# 「deliberately never imports an LLM service」，是本地确定性投影，所以成功的
# 还原仍会同步完成，不会因为批量而在数据里留下隐藏的半成品。

# 跳过原因用稳定 code + 可直接展示的中文，避免前端各自拼文案。
SKIP_MESSAGES: dict[str, str] = {
    "missing_record": "回收记录已不存在（可能已被还原或永久删除）。",
    "source_not_in_outline": "原章节已不在当前大纲中，无法自动还原；请单独选择目标章节还原。",
    "target_has_content": "原章节已有正文，批量还原不会覆盖正文；请单独确认后再还原。",
    "duplicate_source": "同一原章节在本批中已被另一条记录占用，请单独处理这条。",
    "restore_failed": "还原未完成，请刷新后单独重试。",
}


def _skipped(trash_id: str, title: str, reason: str, message: str = "") -> dict:
    return {"trashId": trash_id, "title": title, "reason": reason,
            "message": message or SKIP_MESSAGES.get(reason, "已跳过。")}


def permanently_delete_many(project_id: str, trash_ids: list[str]) -> dict:
    """永久删除选中的多条回收记录。

    逐条删除：一条失败不应拖累其余成功的条目，因此不做整批事务。
    已经被删掉的记录按 `missing` 报告而不是报错——批量重试时这是正常状态。
    """
    deleted: list[str] = []
    missing: list[str] = []
    for trash_id in trash_ids:
        try:
            permanently_delete(project_id, trash_id)
            deleted.append(trash_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            missing.append(trash_id)
    return {"deleted": deleted, "missing": missing}


def batch_restore_chapters(project_id: str, trash_ids: list[str], expected_outline_hash: str) -> dict:
    """把选中的记录各自还原回**它自己的原章节**。

    批量还原刻意不提供「统一目标章节」：多份原稿塞进同一章节会互相覆盖，
    「还原」在这里只有一种无歧义的含义——回到它被移除时所在的章节。
    需要换目标章节时，走单条还原（那条路径允许任选目标）。

    冲突一律**跳过**而不是自动替换：
    - 原章节已不在大纲里 → 跳过，让作者单独指定目标；
    - 原章节已有正文 → 跳过，避免一次点击连环覆盖多处正文；
    - 同一原章节被本批的多条记录指向 → 只还原第一条，其余跳过。

    前两项检查以及「项目是否正在创作」都在动手之前一次性完成，
    所以大纲已变化（或正在创作）时整批**零改动**返回 409，不会留下做了一半的状态。
    """
    if not trash_ids:
        raise HTTPException(400, "请至少选择一条回收记录。")
    placeholders = ",".join("?" for _ in trash_ids)
    with get_conn() as conn:
        require_idle_on_conn(conn, project_id)
        outline = _outline(conn, project_id)
        if outline_hash(outline) != expected_outline_hash:
            raise HTTPException(409, "大纲已变化，请刷新回收站后重试。")
        chapter_nodes = {n.get("id") for n in outline.get("nodes", []) if n.get("type") == "chapter"}
        records = {r["id"]: r for r in conn.execute(
            f"SELECT id, source_chapter_id, title FROM chapter_trash WHERE project_id = ? AND id IN ({placeholders})",
            (project_id, *trash_ids),
        )}
        bodies = {r["id"]: (r["content"] or "") for r in conn.execute(
            "SELECT id, content FROM chapters WHERE project_id = ?", (project_id,),
        )}

    empty_content_hash = chapter_commits.sha256_hex("")
    claimed: set[str] = set()
    restored: list[dict] = []
    skipped: list[dict] = []
    for trash_id in trash_ids:
        row = records.get(trash_id)
        if row is None:
            skipped.append(_skipped(trash_id, "", "missing_record"))
            continue
        title = row["title"] or "未命名章节"
        source_id = str(row["source_chapter_id"] or "")
        if source_id not in chapter_nodes:
            skipped.append(_skipped(trash_id, title, "source_not_in_outline"))
            continue
        if source_id in claimed:
            skipped.append(_skipped(trash_id, title, "duplicate_source"))
            continue
        if (bodies.get(source_id, "") or "").strip():
            skipped.append(_skipped(trash_id, title, "target_has_content"))
            continue
        try:
            result = restore_chapter(
                project_id, trash_id, target_chapter_id=source_id,
                expected_outline_hash=expected_outline_hash,
                target_content_hash=empty_content_hash, confirm_replace=False,
            )
        except HTTPException as exc:
            skipped.append(_skipped(trash_id, title, "restore_failed", str(exc.detail or "")))
            continue
        # 还原后该章节已有正文：本批里再指向它的记录必须跳过，否则会覆盖刚还原的正文。
        claimed.add(source_id)
        restored.append({"trashId": trash_id, "chapterId": result["chapterId"], "title": title})
    return {"restored": restored, "skipped": skipped, "outlineHash": expected_outline_hash}

