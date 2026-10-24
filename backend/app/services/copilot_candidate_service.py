"""Human-controlled Novel Copilot candidates."""

from __future__ import annotations

import difflib
import json
from typing import Any

from app.dao import chapter_rewrites as rewrites_dao
from app.dao import chapters as chapters_dao
from app.dao import memory_fts as memory_fts_dao
from app.dao import projects as projects_dao
from app.db import get_conn, utc_today
from app.models.schemas import ChatRequest
from app.services import ai_service
from app.services.style_engine import profile_service as style_profile_service
from app.services.style_engine import transition_service
from app.services.copilot_action_service import prose_edit_mode
from app.llm.audit import record_llm_postprocess
from app.utils.word_count import count_words

EDIT_ACTIONS = {"continue", "rewrite", "expand", "condense", "style", "character", "emotion", "pacing"}


def _diff(original: str, rewritten: str) -> list[dict[str, Any]]:
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, rewritten).get_opcodes():
        if tag != "equal":
            changes.append({"type": tag, "original": original[i1:i2], "candidate": rewritten[j1:j2]})
    return changes


def _validate_scope(project_id: str, chapter_id: str) -> dict[str, Any]:
    if not projects_dao.project_exists(project_id):
        raise ValueError("project not found")
    chapter = chapters_dao.get_chapter(chapter_id, project_id) if chapter_id else None
    if not chapter:
        raise ValueError("chapter not found")
    return chapter


def _present(record: dict[str, Any]) -> dict[str, Any]:
    original = str(record.get("original") or "")
    rewritten = str(record.get("rewritten") or "")
    reason = str(record.get("reason") or "")
    metadata = {}
    if reason.startswith('{"copilot":'):
        metadata = json.loads(reason)
        reason = f"Copilot action: {record.get('rewrite_type', 'rewrite')}"
    return {**record, "reason": reason, "candidate": rewritten, "diff": _diff(original, rewritten),
            "stylePlanId": metadata.get("style_plan_id"), "stylePlanRevision": metadata.get("style_plan_revision")}


async def create_candidate(*, project_id: str, chapter_id: str, action: str,
                           selection_text: str = "", current_text: str = "",
                           instruction: str = "", style_plan_id: str | None = None,
                           style_plan_revision: int | None = None) -> dict[str, Any]:
    chapter = _validate_scope(project_id, chapter_id)
    if action not in EDIT_ACTIONS:
        raise ValueError("action does not produce an editable candidate")
    original = selection_text or ("" if action == "continue" else current_text)
    if not original and action != "continue":
        raise ValueError("selection or current text is required")
    saved_content = str(chapter.get("content") or "")
    if current_text and current_text != saved_content:
        # 带上两侧字数：智能审校的「秒拒」（<100ms、未调模型）只能靠这一点区分
        # 「客户端持有旧正文」与「正文被其他写入改过」。全文不进日志/提示，只给长度。
        raise ValueError(
            f"当前正文尚未保存或已更新（客户端 {len(current_text)} 字 / 服务端 {len(saved_content)} 字），"
            "请先保存并同步最新正文后再修改；本次未调用模型。"
        )
    if selection_text and selection_text not in saved_content:
        raise ValueError(
            f"选区正文已更新（选区 {len(selection_text)} 字，服务端正文 {len(saved_content)} 字），"
            "请重新选择要修改的文字；本次未调用模型。"
        )
    prompt = instruction or f"{action}: return only the replacement text, without explanation."
    live_contract = style_profile_service.build_generation_contract(project_id)
    token = transition_service.plan_token(live_contract)
    if style_plan_id is not None or style_plan_revision is not None:
        if style_plan_id != token["stylePlanId"] or style_plan_revision != token["stylePlanRevision"]:
            raise transition_service.TransitionRevisionConflict()
    current_style_plan_id = token["stylePlanId"]
    current_style_plan_revision = token["stylePlanRevision"]
    # A whole-chapter rebuild must not feed the rejected draft back as plot
    # evidence. Keep it server-side for diff/Accept; ordinary selected edits
    # still need their source text.
    edit_mode = prose_edit_mode(instruction, action)
    effective_current = current_text or str(chapter.get("content") or "")
    rebuild = (
        action == "rewrite"
        and (not selection_text or selection_text == effective_current)
        and edit_mode == "rebuild"
    )
    req = ChatRequest(
        projectId=project_id, chapterId=chapter_id,
        currentText="" if rebuild else effective_current,
        selectionText="" if rebuild else selection_text, action=action,
        messages=[{"role": "user", "content": prompt if rebuild else f"{prompt}\n\nOriginal text:\n{original}"}],
    )
    req._style_contract = live_contract
    response = await ai_service.chat_reply(req, **({"regenerate_from_outline": True} if rebuild else {}))
    rewritten = str(response.get("reply") or "").strip()
    failure = None
    if response.get("mode") in {"failed", "fallback"} or response.get("is_local_suggestion"):
        failure = ("copilot_provider_failed", "模型未完成正文修改，未创建候选，原文保持不变。")
    elif not rewritten:
        failure = ("copilot_empty_output", "模型返回空正文，未创建候选，原文保持不变。")
    elif original and rewritten == original.strip():
        failure = ("copilot_no_change", "模型未产生实际修改，未创建候选，原文保持不变。")
    trace_id = response.get("trace_id") or ""
    record_llm_postprocess(trace_id, status="failed" if failure else "success", stage="candidate_create",
                           code=failure[0] if failure else "", response_shape="prose")
    if failure:
        # 附机器可读码：智能审校批量修改据此区分“本条只是没改动/模型没跑成（正文未动，可跳过继续）”
        # 与“正文已变（必须停下）”。旧调用方仍能拿到同一句中文提示，不受影响。
        error = ValueError(failure[1] + (f"诊断编号：{trace_id}。" if trace_id else ""))
        error.copilot_code = failure[0]  # type: ignore[attr-defined]
        raise error
    reason = f"Copilot action: {action}"
    # Reuse the existing reason column; only policy/scope are needed to keep
    # a manual regeneration on the same path. No schema or prompt archive.
    stored_reason = json.dumps({"copilot": True, "edit_mode": edit_mode,
                               "whole_chapter": not selection_text or selection_text == effective_current,
                               "style_plan_id": current_style_plan_id,
                               "style_plan_revision": current_style_plan_revision}, ensure_ascii=False)
    rewrite_id = rewrites_dao.insert_rewrite(
        project_id, chapter_id, action, original=original, rewritten=rewritten, reason=stored_reason,
    )
    saved = rewrites_dao.get_rewrite(rewrite_id) or {}
    return _present({**saved, "action": action, "explanation": reason,
                     "llm_context": response.get("context", {})})


def list_candidates(project_id: str, chapter_id: str = "") -> list[dict[str, Any]]:
    if not projects_dao.project_exists(project_id):
        raise ValueError("project not found")
    return [_present(item) for item in rewrites_dao.list_rewrites(project_id, chapter_id=chapter_id)]


def reject_candidate(rewrite_id: str) -> dict[str, Any] | None:
    record = rewrites_dao.get_rewrite(rewrite_id)
    if not record:
        return None
    if record["status"] != rewrites_dao.REWRITE_STATUS_PROPOSED:
        raise ValueError("candidate is no longer pending")
    rewrites_dao.update_status(rewrite_id, rewrites_dao.REWRITE_STATUS_REJECTED)
    return _present(rewrites_dao.get_rewrite(rewrite_id) or record)


def accept_candidate(rewrite_id: str) -> dict[str, Any] | None:
    record = rewrites_dao.get_rewrite(rewrite_id)
    if not record:
        return None
    with get_conn() as conn:
        # Validate and write under the same lock, including candidate status.
        # Two Accept requests cannot both append/overwrite from an old snapshot.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM chapter_rewrites WHERE id = ?", (rewrite_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        if record["status"] != rewrites_dao.REWRITE_STATUS_PROPOSED:
            raise ValueError("此候选已处理，请基于当前正文发起新的修改。")
        chapter = conn.execute("SELECT * FROM chapters WHERE project_id=? AND id=?",
                               (record["project_id"], record["chapter_id"])).fetchone()
        if not chapter:
            raise ValueError("chapter not found")
        chapter = dict(chapter)
        content = str(chapter.get("content") or "")
        original = str(record.get("original") or "")
        replacement = str(record.get("rewritten") or "")
        metadata = {}
        try:
            parsed = json.loads(record.get("reason") or "")
            if isinstance(parsed, dict):
                metadata = parsed
        except (ValueError, TypeError):
            pass
        # Known chapter IDs do not make generated text under an old weight current.
        transition_service.validate_plan_on_conn(
            conn, record["project_id"], metadata.get("style_plan_id", ""),
            metadata.get("style_plan_revision", 0),
        )
        if original:
            if original not in content or (metadata.get("whole_chapter") and original != content):
                raise ValueError("正文已更新，此候选基于旧版本，未覆盖当前正文。请点击重新生成回复，基于最新正文调整。")
            content = content.replace(original, replacement, 1)
        else:
            content = content + ("\n" if content and not content.endswith("\n") else "") + replacement
        conn.execute(
            "UPDATE chapters SET content = ?, word_count = ?, updated_at = ? WHERE id = ? AND project_id = ?",
            (content, count_words(content), utc_today(), record["chapter_id"], record["project_id"]),
        )
        # 与手工保存 (PUT /chapters/{id}) 保持一致：正文变更后在同一事务内重算项目总字数，
        # 避免 Copilot 接受后项目汇总与活动章节合计不一致。
        projects_dao._update_project_word_count_on_conn(conn, record["project_id"], utc_today())
        if content.strip():
            transition_service.record_chapter_completion_on_conn(
                    conn,
                    record["project_id"],
                    record["chapter_id"],
                    metadata.get("style_plan_id", ""),
                    metadata.get("style_plan_revision", 0),
                    source="copilot_accept",
                )
        conn.execute("UPDATE chapter_rewrites SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
                     (rewrites_dao.REWRITE_STATUS_APPLIED, rewrites_dao._utc_iso(), rewrites_dao._utc_iso(), rewrite_id))

    # 派生 FTS 索引（非关键路径）：与手工保存同样处理——失败静默跳过，绝不回滚已接受的正文。
    # 后续投影重放会基于已提交的正文重建，因此这里只是尽力而为的即时同步。
    try:
        memory_fts_dao.sync_chapter_fts(record["project_id"], record["chapter_id"])
    except Exception:  # noqa: BLE001
        pass

    return {**_present(rewrites_dao.get_rewrite(rewrite_id) or record), "applied_content": content,
            "styleTransition": transition_service.get_transition_progress(record["project_id"])}


async def regenerate_candidate(rewrite_id: str, instruction: str = "") -> dict[str, Any] | None:
    record = rewrites_dao.get_rewrite(rewrite_id)
    if not record:
        return None
    if record["status"] != rewrites_dao.REWRITE_STATUS_PROPOSED:
        raise ValueError("candidate is no longer pending")
    metadata = {}
    try:
        metadata = json.loads(record.get("reason") or "")
    except (ValueError, TypeError):
        pass  # Legacy candidates have a plain-text reason.
    if not isinstance(metadata, dict):
        metadata = {}
    if not instruction:
        instruction = {
            "rebuild": "按当前剧情弧调整正文，不要出现其他弧的内容。",
            "correct": "按照最新大纲纠正正文。",
        }.get(metadata.get("edit_mode"), "")
    chapter = _validate_scope(record['project_id'], record['chapter_id'])
    current_text = str(chapter.get('content') or '')
    if record['original'] and (
        record['original'] not in current_text
        or metadata.get('whole_chapter') and record['original'] != current_text
    ):
        raise ValueError("原文已经变化，请基于当前正文重新发起修改。")
    return await create_candidate(
        project_id=record["project_id"], chapter_id=record["chapter_id"],
        action=record["rewrite_type"], selection_text=record["original"],
        current_text=current_text, instruction=instruction,
    )
