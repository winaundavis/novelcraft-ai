"""Style contracts through real HTTP/save paths; every provider is a mock."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from main import app, _request_windows
from app.db import default_style_config, get_conn, json_dump
from app.dao import chapters, chapter_rewrites
from app.services import ai_service, copilot_candidate_service as copilot
from app.services import novel_generation_service as ngs
from app.services.style_engine import profile_service as profiles, transition_service as transitions
from app.models.schemas import ContinueWritingRequest


@pytest.fixture
def workspace():
    _request_windows.clear()
    with TestClient(app) as client:
        p = client.post('/api/projects/', json={'name': 'style-runtime', 'genre': 'xuanhuan'}).json()['data']['id']
        yield client, p


def plan(client, pid, **extra):
    response = client.put(f'/api/projects/{pid}/style-transition', json={
        'pace': 'fast', 'targetStyle': {'presetKey': 'emotional_resonance', 'customPrompt': '保留克制对话'}, **extra})
    assert response.status_code == 200, response.text
    return response.json()['data']


def token(t):
    return {'stylePlanId': t['planId'], 'stylePlanRevision': t['revision']}


@pytest.mark.parametrize('count', [0, -1, 201, 1.5, True, '60'])
def test_http_rejects_invalid_count(workspace, count):
    client, pid = workspace
    response = client.put(f'/api/projects/{pid}/style-transition', json={'pace': 'custom', 'targetChapters': count})
    assert response.status_code == 422
    assert transitions.get_transition(pid) is None


def test_custom_prompt_only_uses_current_project_style_as_target(workspace):
    client, pid = workspace
    current_style = default_style_config('poetic')
    current_style['avgSentenceLength'] = 33
    with get_conn() as conn:
        conn.execute('UPDATE projects SET style_config=? WHERE id=?', (json_dump(current_style), pid))

    response = client.put(f'/api/projects/{pid}/style-transition', json={
        'pace': 'fast',
        'targetStyle': {'presetKey': '', 'customPrompt': '减少心理独白，保留克制对话。'},
    })

    assert response.status_code == 200, response.text
    target = response.json()['data']['targetSnapshot']
    assert target['presetKey'] == 'poetic'
    assert target['preset']['name'] == 'Poetic'
    assert target['preset']['avgSentenceLength'] == 33
    assert target['customPrompt'] == '减少心理独白，保留克制对话。'


def test_custom_prompt_only_rejects_unresolvable_current_style(workspace):
    client, pid = workspace
    with get_conn() as conn:
        conn.execute('UPDATE projects SET style_config=? WHERE id=?', (json_dump({'name': 'Unknown Style'}), pid))

    response = client.put(f'/api/projects/{pid}/style-transition', json={
        'pace': 'fast',
        'targetStyle': {'presetKey': '', 'customPrompt': '保留短句。'},
    })

    assert response.status_code == 400
    assert '项目当前文风无法识别' in response.json()['detail']


def test_explicit_no_plan_token_rejects_plan_created_before_save(workspace):
    client, pid = workspace
    late_plan = plan(client, pid)
    before = client.get(f'/api/projects/{pid}').json()['data']['currentWordCount']

    response = client.put(f'/api/projects/{pid}/chapters/late-generated', json={
        'content': '使用无计划契约生成的正文',
        'stylePlanId': '',
        'stylePlanRevision': 0,
    })

    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'style_plan_conflict'
    assert chapters.get_chapter('late-generated', pid) is None
    assert transitions.get_transition(pid)['planId'] == late_plan['planId']
    assert transitions.get_transition(pid)['completedCount'] == 0
    assert client.get(f'/api/projects/{pid}').json()['data']['currentWordCount'] == before


def test_missing_plan_token_remains_compatible_with_manual_save(workspace):
    client, pid = workspace
    plan(client, pid)

    response = client.put(f'/api/projects/{pid}/chapters/manual', json={'content': '手工保存正文'})

    assert response.status_code == 200, response.text
    assert chapters.get_chapter('manual', pid)['content'] == '手工保存正文'


def test_stale_token_auto_write_start_returns_conflict_without_creating_job(workspace, monkeypatch):
    from app.api import novel_jobs as novel_jobs_api

    client, pid = workspace
    stale = plan(client, pid)
    plan(client, pid, pace='slow')
    start = Mock()
    monkeypatch.setattr(novel_jobs_api, 'start_generation_job', start)

    response = client.post('/api/ai/start', json={'projectId': pid, **token(stale)})

    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'style_plan_conflict'
    start.assert_not_called()
    assert ngs.get_project_latest_job(pid) is None


def test_latest_conflict_job_is_visible_but_not_recoverable(workspace):
    client, pid = workspace
    job = ngs.NovelGenerationJob(
        id=f'job-conflict-{pid}', project_id=pid, mode='single', requested_chapter_id=None,
        status='failed', message='style plan changed', target_words=2000, total_chapters=1,
        max_retries=0, checkpoint_type='style_plan_conflict',
        resume_target_json=json.dumps({'chapter_id': 'chapter-1', 'candidateContent': '保留的候选正文'}),
    )
    ngs._save_job(job)

    latest = client.get(f'/api/ai/project/{pid}')
    recoverable = client.get(f'/api/ai/project/{pid}/recoverable')

    assert latest.status_code == 200
    assert latest.json()['data']['checkpoint_type'] == 'style_plan_conflict'
    assert latest.json()['data']['preview_content'] == '保留的候选正文'
    assert recoverable.status_code == 200
    assert all(item['checkpoint_type'] != 'style_plan_conflict' for item in recoverable.json()['data'])


def test_save_conflict_rolls_back_body_words_and_progress(workspace):
    client, pid = workspace
    t = plan(client, pid)
    first = client.put(f'/api/projects/{pid}/chapters/c-{pid}-1', json={'content': '甲乙丙', **token(t)})
    assert first.status_code == 200
    second = client.put(f'/api/projects/{pid}/chapters/c-{pid}-2', json={'content': '后来正文', **token(t)})
    assert second.status_code == 409
    assert second.json()['detail']['code'] == 'style_plan_conflict'
    assert chapters.get_chapter(f'c-{pid}-2', pid) is None
    assert transitions.get_transition(pid)['completedCount'] == 1
    assert client.get(f'/api/projects/{pid}').json()['data']['currentWordCount'] == 3


def test_save_and_generated_commit_roll_back_if_ledger_write_fails(workspace, monkeypatch):
    client, pid = workspace
    t = plan(client, pid)
    with get_conn() as conn:
        conn.execute("CREATE TRIGGER fail_style_progress BEFORE UPDATE ON style_transitions BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END")
    try:
        with pytest.raises(Exception, match='ledger unavailable'):
            ngs._persist_generated_chapter(pid, {'id': f'c-{pid}', 'title': '新章'}, '正文',
                                          commit_meta={'style_plan_id': t['planId'], 'style_plan_revision': 1,
                                                       'story_state_version_before': 1})
        assert chapters.get_chapter(f'c-{pid}', pid) is None
        assert transitions.get_transition(pid)['completedCount'] == 0
        with get_conn() as conn:
            assert conn.execute('SELECT COUNT(*) FROM chapter_commit_history WHERE project_id=?', (pid,)).fetchone()[0] == 0
    finally:
        with get_conn() as conn:
            conn.execute('DROP TRIGGER fail_style_progress')


def test_sse_stale_token_rejected_before_any_provider_call(workspace, monkeypatch):
    client, pid = workspace
    old = plan(client, pid)
    plan(client, pid, pace='slow')
    provider = AsyncMock()
    monkeypatch.setattr(ai_service, 'get_runtime_provider', lambda *_: provider)
    response = client.post('/api/ai/continue-writing', json={'projectId': pid, 'chapterId': 'c', **token(old)})
    assert response.status_code == 409
    assert provider.mock_calls == []


def test_continuation_ignores_cached_style_and_exposes_current_token(workspace):
    client, pid = workspace
    t = plan(client, pid)
    req = ContinueWritingRequest(projectId=pid, chapterId='c', styleTone='OBSOLETE STYLE', **token(t))
    prompt = ai_service._build_continue_prompt(req)
    assert 'OBSOLETE STYLE' not in prompt
    assert '保留克制对话' in prompt
    assert 'Emotional Resonance' in prompt
    assert req._style_contract == profiles.build_generation_contract(pid)
    assert req.stylePlanId == t['planId']
    assert req._style_contract['transition']['weight'] == pytest.approx(1 / 9)


@pytest.mark.asyncio
async def test_copilot_uses_same_contract_and_stale_accept_retains_candidate(workspace, monkeypatch):
    client, pid = workspace
    cid = f'c-{pid}'
    client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '原始正文'})
    t = plan(client, pid)
    mock = AsyncMock(return_value={'reply': '新的正文', 'mode': 'llm'})
    monkeypatch.setattr(ai_service, 'chat_reply', mock)
    candidate = await copilot.create_candidate(project_id=pid, chapter_id=cid, action='rewrite', current_text='原始正文')
    assert mock.await_args.args[0]._style_contract == profiles.build_generation_contract(pid)
    assert candidate['stylePlanId'] == t['planId']
    plan(client, pid, pace='medium')
    response = client.post(f"/api/copilot/candidates/{candidate['id']}/accept")
    assert response.status_code == 409
    assert chapters.get_chapter(cid, pid)['content'] == '原始正文'
    assert chapter_rewrites.get_rewrite(candidate['id'])['status'] == 'proposed'
    assert transitions.get_transition(pid)['completedCount'] == 0
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_copilot_empty_chapter_accept_advances_once(workspace, monkeypatch):
    client, pid = workspace
    cid = f'c-{pid}'
    client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': ''})
    plan(client, pid)
    monkeypatch.setattr(ai_service, 'chat_reply', AsyncMock(return_value={'reply': '新章正文', 'mode': 'llm'}))
    candidate = await copilot.create_candidate(project_id=pid, chapter_id=cid, action='continue')
    accepted = copilot.accept_candidate(candidate['id'])
    assert accepted['applied_content'] == '新章正文'
    assert accepted['styleTransition']['completedCount'] == 1
    with pytest.raises(ValueError):
        copilot.accept_candidate(candidate['id'])
    assert transitions.get_transition(pid)['completedCount'] == 1


@pytest.mark.asyncio
async def test_generation_conflict_stops_without_retry_and_survives_restart(workspace, monkeypatch):
    client, pid = workspace
    cid = f'c-{pid}'
    outline = {'nodes': [{'id': cid, 'type': 'chapter', 'title': '第1章', 'order': 1}]}
    client.patch(f'/api/projects/{pid}', json={'outline': outline})
    plan(client, pid)
    job = ngs.NovelGenerationJob(id=f'job-{pid}', project_id=pid, mode='all', requested_chapter_id=None,
                                 status='running', message='', target_words=2000, total_chapters=1, max_retries=2)
    ngs._save_job(job)
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs['style_contract'])
        # User replaces the plan during the single mocked provider response.
        transitions.start_transition(pid, 'slow')
        return {'content': '保留的候选正文', 'used_fallback': False, 'provider': 'mock', 'model': 'mock', 'last_source': 'llm'}

    async def persist(**kwargs):
        t = calls[-1]['transition']
        ngs._persist_generated_chapter(pid, kwargs['chapter'], kwargs['result']['content'],
                                      commit_meta={'style_plan_id': t['planId'], 'style_plan_revision': t['revision']})
        return ''

    monkeypatch.setattr(ngs, '_generate_chapter_with_expansion', generate)
    monkeypatch.setattr(ngs, '_persist_chapter_and_sync_memory', persist)
    monkeypatch.setattr(ngs, '_build_memory_context', lambda **_: {})
    monkeypatch.setattr(ngs, '_run_self_check_loop', AsyncMock(return_value={'content': '保留的候选正文',
                         'fact_check_count': 0, 'rewrite_count': 0, 'hook_miss_count': 0, 'quality_debt_count': 0}))
    await ngs._run_generation_job(job.id)
    recovered = ngs._load_job(job.id)
    assert len(calls) == 1
    assert recovered.retry_count == 0
    assert recovered.checkpoint_type == 'style_plan_conflict'
    assert recovered.preview_content == '保留的候选正文'
    assert not chapters.get_chapter(cid, pid)['content']
    assert transitions.get_transition(pid)['completedCount'] == 0
    with pytest.raises(RuntimeError, match='旧候选'):
        ngs.resume_job(job.id)


def test_concurrent_generation_commits_share_one_revision(workspace):
    client, pid = workspace
    t = plan(client, pid)
    barrier = Barrier(2)

    def submit(index):
        barrier.wait(timeout=5)
        try:
            ngs._persist_generated_chapter(pid, {'id': f'{pid}-{index}', 'title': '并发新章'}, '新正文',
                                          commit_meta={'style_plan_id': t['planId'], 'style_plan_revision': 1,
                                                       'story_state_version_before': 1})
            return 'saved'
        except transitions.TransitionRevisionConflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, (1, 2)))
    assert sorted(outcomes) == ['conflict', 'saved']
    assert len(chapters.list_chapters(pid)) == 1
    assert transitions.get_transition(pid)['completedCount'] == 1
    assert client.get(f'/api/projects/{pid}').json()['data']['currentWordCount'] == 3
    with get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM chapter_commit_history WHERE project_id=?', (pid,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_actual_copilot_request_contains_frozen_style(workspace, monkeypatch):
    from types import SimpleNamespace
    client, pid = workspace
    cid = f'c-{pid}'
    client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '原句'})
    plan(client, pid)
    mock = AsyncMock(return_value='新的句子')
    provider = SimpleNamespace(chat=mock, provider='mock', model='mock')
    monkeypatch.setattr(ai_service, 'get_runtime_provider', lambda *_: provider)
    await copilot.create_candidate(project_id=pid, chapter_id=cid, action='emotion', current_text='原句')
    messages = mock.await_args.args[0]
    contract = profiles.contract_to_prompt_block(profiles.build_generation_contract(pid))
    assert any(message['role'] == 'system' and message['content'] == contract for message in messages)
    assert mock.await_count == 1


def test_imported_chapters_do_not_advance_or_advance_on_later_save(workspace):
    from app.api.imports import _upsert_import_project
    client, pid = workspace
    plan(client, pid)
    cid = f'import-{pid}'
    _upsert_import_project(project_id=pid, project_name=None, owner_id='local-dev', scan_result={
        'folderName': 'mock-import', 'folderPath': 'mock-import', 'totalWords': 4, 'analysis': {},
        'chapters': [{'id': cid, 'title': '导入章', 'order': 1, 'content': '导入正文', 'wordCount': 4}]})
    state = transitions.get_transition(pid)
    assert state['completedCount'] == 0
    assert cid in state['plan']['excludedChapterIds']
    response = client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '导入章修改', **token(state)})
    assert response.status_code == 200
    assert transitions.get_transition(pid)['completedCount'] == 0


def test_restored_chapter_under_new_id_does_not_advance(workspace):
    from app.services import chapter_trash_service as trash
    from app.dao.chapter_commits import sha256_hex
    client, pid = workspace
    original, target = f'{pid}-original', f'{pid}-target'
    outline = {'nodes': [{'id': cid, 'type': 'chapter', 'title': '章节', 'order': i} for i, cid in enumerate((original, target), 1)]}
    client.patch(f'/api/projects/{pid}', json={'outline': outline})
    client.put(f'/api/projects/{pid}/chapters/{original}', json={'content': '原有正文'})
    plan(client, pid)
    trash.recycle_chapter(pid, original)
    with get_conn() as conn:
        tid = conn.execute('SELECT id FROM chapter_trash WHERE project_id=?', (pid,)).fetchone()[0]
    trash.restore_chapter(pid, tid, target_chapter_id=target, expected_outline_hash=trash.outline_hash(outline),
                          target_content_hash=sha256_hex(''))
    current = transitions.get_transition(pid)
    assert current['completedCount'] == 0
    assert target in current['plan']['excludedChapterIds']
    response = client.put(f'/api/projects/{pid}/chapters/{target}', json={'content': '还原正文修改', **token(current)})
    assert response.status_code == 200
    assert transitions.get_transition(pid)['completedCount'] == 0


@pytest.mark.asyncio
async def test_baseline_candidate_cannot_use_revision_consumed_by_other_chapter(workspace, monkeypatch):
    client, pid = workspace
    cid = f'baseline-{pid}'
    client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '原有正文'})
    t = plan(client, pid)
    provider = AsyncMock(return_value={'reply': '旧权重候选', 'mode': 'llm'})
    monkeypatch.setattr(ai_service, 'chat_reply', provider)
    candidate = await copilot.create_candidate(project_id=pid, chapter_id=cid, action='rewrite', current_text='原有正文')
    client.put(f'/api/projects/{pid}/chapters/new-{pid}', json={'content': '推进进度', **token(t)})

    response = client.post(f"/api/copilot/candidates/{candidate['id']}/accept")
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'style_plan_conflict'
    assert chapters.get_chapter(cid, pid)['content'] == '原有正文'
    assert chapter_rewrites.get_rewrite(candidate['id'])['status'] == 'proposed'
    assert transitions.get_transition(pid)['completedCount'] == 1
    assert provider.await_count == 1


def test_generated_baseline_rewrite_rejects_consumed_revision(workspace):
    client, pid = workspace
    cid = f'baseline-{pid}'
    client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '原有正文'})
    t = plan(client, pid)
    client.put(f'/api/projects/{pid}/chapters/new-{pid}', json={'content': '推进进度', **token(t)})
    with pytest.raises(transitions.TransitionRevisionConflict):
        ngs._persist_generated_chapter(pid, {'id': cid, 'title': '旧章'}, '旧权重生成',
                                      commit_meta={'style_plan_id': t['planId'], 'style_plan_revision': t['revision'],
                                                   'story_state_version_before': 1})
    assert chapters.get_chapter(cid, pid)['content'] == '原有正文'
    with get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM chapter_commit_history WHERE project_id=?', (pid,)).fetchone()[0] == 0


def test_continuation_save_requires_current_revision_but_identical_resave_is_idempotent(workspace):
    client, pid = workspace
    t = plan(client, pid)
    cid = f'completed-{pid}'
    saved = {'content': '第一次保存', **token(t)}
    assert client.put(f'/api/projects/{pid}/chapters/{cid}', json=saved).status_code == 200
    assert client.put(f'/api/projects/{pid}/chapters/{cid}', json=saved).status_code == 200
    response = client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '不同权重的新内容', **token(t)})
    assert response.status_code == 409
    assert chapters.get_chapter(cid, pid)['content'] == '第一次保存'
    assert transitions.get_transition(pid)['completedCount'] == 1
    # Ordinary manual edits still work; they never count this chapter twice.
    assert client.put(f'/api/projects/{pid}/chapters/{cid}', json={'content': '手工修改'}).status_code == 200
    assert transitions.get_transition(pid)['completedCount'] == 1
