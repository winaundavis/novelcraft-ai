"""Project-owned conversation storage. Never hold a SQLite transaction across AI work."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import get_conn, json_dump
from app.models.schemas import gen_id


def now():
    return datetime.now(timezone.utc).isoformat()


def _owned(conn, project_id, conversation_id):
    row = conn.execute('SELECT * FROM chat_conversations WHERE id = ? AND project_id = ?',
                       (conversation_id, project_id)).fetchone()
    if row is None:
        raise HTTPException(404, '对话不存在。')
    return {**dict(row), 'archived': bool(row['archived'])}


def _idle(conn, conversation_id):
    if conn.execute("SELECT 1 FROM chat_messages WHERE conversation_id = ? AND status = 'pending'", (conversation_id,)).fetchone():
        raise HTTPException(409, '这次对话仍在回复中，请稍后操作。')


def _touch(conn, conversation_id):
    conn.execute('UPDATE chat_conversations SET revision = revision + 1, updated_at = ? WHERE id = ?', (now(), conversation_id))


def create(project_id, title='新对话'):
    cid, stamp = gen_id(), now()
    with get_conn() as conn:
        conn.execute('INSERT INTO chat_conversations(id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                     (cid, project_id, title.strip()[:100] or '新对话', stamp, stamp))
    return get(project_id, cid)


def list_conversations(project_id, archived=False):
    with get_conn() as conn:
        rows = conn.execute('SELECT * FROM chat_conversations WHERE project_id = ? AND archived = ? ORDER BY updated_at DESC, rowid DESC',
                            (project_id, int(archived))).fetchall()
    return [{**dict(row), 'archived': bool(row['archived'])} for row in rows]


def get(project_id, conversation_id):
    with get_conn() as conn:
        result = _owned(conn, project_id, conversation_id)
        rows = conn.execute('SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY sequence', (conversation_id,)).fetchall()
    messages = []
    for row in rows:
        metadata = json.loads(row['metadata_json'])
        candidate = metadata.get('candidate')
        # Snapshots preserve history; live candidate status decides which actions are available.
        if candidate and candidate.get('id'):
            if metadata.get('kind') == 'copilot':
                from app.dao.chapter_rewrites import get_rewrite
                from app.services.copilot_candidate_service import _present
                current = get_rewrite(candidate['id'])
                metadata['candidate'] = _present(current) if current and current['project_id'] == project_id else None
            else:
                from app.dao.outline_candidates import get_outline_candidate
                current = get_outline_candidate(candidate['id'])
                metadata['candidate'] = {**candidate, 'status': current['status']} if current and current['project_id'] == project_id else None
        messages.append({'id': row['id'], 'role': row['role'], 'content': row['content'],
                         'status': row['status'], 'timestamp': row['created_at'], 'metadata': metadata})
    return {**result, 'messages': messages}


def update(project_id, conversation_id, *, title=None, archived=None):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _owned(conn, project_id, conversation_id)
        _idle(conn, conversation_id)
        if title is not None:
            conn.execute('UPDATE chat_conversations SET title = ? WHERE id = ?', (title.strip()[:100] or '新对话', conversation_id))
        if archived is not None:
            conn.execute('UPDATE chat_conversations SET archived = ? WHERE id = ?', (int(archived), conversation_id))
        _touch(conn, conversation_id)
    return get(project_id, conversation_id)


def _delete_rows(conn, ids):
    for cid in ids:
        conn.execute('DELETE FROM chat_requests WHERE conversation_id = ?', (cid,))
        conn.execute('DELETE FROM chat_messages WHERE conversation_id = ?', (cid,))
        conn.execute('DELETE FROM chat_conversations WHERE id = ?', (cid,))


def delete(project_id, conversation_id):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _owned(conn, project_id, conversation_id)
        _idle(conn, conversation_id)
        _delete_rows(conn, [conversation_id])


def delete_project_on_conn(conn, project_id):
    """Validate and remove conversations inside the caller's transaction."""
    ids = [row[0] for row in conn.execute('SELECT id FROM chat_conversations WHERE project_id = ?', (project_id,))]
    for cid in ids:
        _idle(conn, cid)
    _delete_rows(conn, ids)


def delete_project(project_id):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        delete_project_on_conn(conn, project_id)


def begin_turn(project_id, conversation_id, request_id, content, regenerate_id=None, kind='chat', operation=None):
    if not request_id or len(request_id) > 100:
        raise HTTPException(422, '对话请求缺少有效编号。')
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conversation = _owned(conn, project_id, conversation_id)
        previous = conn.execute('SELECT * FROM chat_requests WHERE conversation_id = ? AND request_id = ?', (conversation_id, request_id)).fetchone()
        if previous:
            if previous['status'] == 'pending':
                raise HTTPException(409, '这条消息仍在处理中，请等待回复。')
            return {'replay': json.loads(previous['result_json'])}
        if conversation['archived']:
            raise HTTPException(409, '请先恢复归档对话，再继续聊天。')
        _idle(conn, conversation_id)
        rows = conn.execute('SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY sequence', (conversation_id,)).fetchall()
        if regenerate_id:
            index = next((i for i, row in enumerate(rows) if row['id'] == regenerate_id and row['role'] == 'assistant'), -1)
            if index < 1:
                raise HTTPException(404, '回复不存在。')
            if json.loads(rows[index]['metadata_json']).get('kind', 'chat') != kind:
                raise HTTPException(409, '请使用这条回复原有的操作类型重新生成。')
            history = rows[:index]
            assistant_id = regenerate_id
            # Keep the previous reply visible until its replacement has completed.
            conn.execute("UPDATE chat_messages SET status = 'pending' WHERE id = ?", (assistant_id,))
        else:
            if not content.strip() or len(content) > 20000:
                raise HTTPException(422, '消息不能为空，且不能超过 20000 字符。')
            history = rows
            uid, assistant_id, stamp = gen_id(), gen_id(), now()
            conn.execute("INSERT INTO chat_messages(id, conversation_id, role, content, status, created_at) VALUES (?, ?, 'user', ?, 'completed', ?)",
                         (uid, conversation_id, content, stamp))
            conn.execute("INSERT INTO chat_messages(id, conversation_id, role, content, status, metadata_json, created_at) VALUES (?, ?, 'assistant', '', 'pending', ?, ?)",
                         (assistant_id, conversation_id, json_dump({'kind': kind, 'operation': operation}), stamp))
            if not rows and conversation['title'] == '新对话':
                conn.execute('UPDATE chat_conversations SET title = ? WHERE id = ?', (' '.join(content.split())[:40], conversation_id))
        history = [{'role': row['role'], 'content': row['content']} for row in history if row['status'] == 'completed']
        if not regenerate_id:
            history.append({'role': 'user', 'content': content})
        conn.execute("INSERT INTO chat_requests(conversation_id, request_id, assistant_id, status) VALUES (?, ?, ?, 'pending')",
                     (conversation_id, request_id, assistant_id))
        _touch(conn, conversation_id)
    return {'assistant_id': assistant_id, 'history': history}


def finish_turn(conversation_id, request_id, result, *, kind='chat', interrupted=False):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        ticket = conn.execute('SELECT * FROM chat_requests WHERE conversation_id = ? AND request_id = ?', (conversation_id, request_id)).fetchone()
        if ticket is None:
            return result
        old = conn.execute('SELECT metadata_json FROM chat_messages WHERE id = ?', (ticket['assistant_id'],)).fetchone()
        old_meta = json.loads(old['metadata_json']) if old else {}
        # Conversation content is durable; assembled project prompts/context blocks are not duplicated here.
        result = {k: v for k, v in result.items() if k in {
            'reply', 'mode', 'intent', 'action', 'error_code', 'request_id', 'trace_id',
            'outline_failure_stage', 'outline_failure_code', 'retryable', 'is_local_suggestion',
            'provider', 'model', 'candidate',
        }}
        result = {**result, 'conversationId': conversation_id, 'assistantMessageId': ticket['assistant_id']}
        status = 'interrupted' if interrupted else 'failed' if result.get('mode') == 'failed' else 'completed'
        metadata = {**result, 'kind': kind, 'operation': old_meta.get('operation')}
        conn.execute('UPDATE chat_messages SET content = ?, status = ?, metadata_json = ? WHERE id = ?',
                     (result.get('reply', ''), status, json_dump(metadata), ticket['assistant_id']))
        conn.execute('UPDATE chat_requests SET status = ?, result_json = ? WHERE conversation_id = ? AND request_id = ?',
                     (status, json_dump(result), conversation_id, request_id))
        _touch(conn, conversation_id)
    return result


def fail_turn(conversation_id, request_id, *, kind='chat', interrupted=False):
    return finish_turn(conversation_id, request_id, {
        'reply': '回复未完成。已保留对话，你可以手动重新生成。', 'mode': 'failed',
        'error_code': 'chat_interrupted' if interrupted else 'chat_request_failed', 'retryable': True,
    }, kind=kind, interrupted=interrupted)


def recover_pending():
    with get_conn() as conn:
        tickets = conn.execute("SELECT r.*, m.metadata_json FROM chat_requests r JOIN chat_messages m ON m.id = r.assistant_id WHERE m.status = 'pending' ORDER BY r.rowid DESC").fetchall()
    recovered = set()
    for ticket in tickets:
        if ticket['assistant_id'] in recovered:
            continue
        fail_turn(ticket['conversation_id'], ticket['request_id'], kind=json.loads(ticket['metadata_json']).get('kind', 'chat'), interrupted=True)
        recovered.add(ticket['assistant_id'])
