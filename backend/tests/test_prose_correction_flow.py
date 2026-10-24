"""Exercise the actual chat candidate -> provider -> Accept boundary, offline."""

from unittest.mock import AsyncMock
import json

import pytest

from app.dao import chapters, projects, chapter_rewrites
from app.models.schemas import ChatRequest, gen_id
from app.services import ai_service, copilot_candidate_service as service


@pytest.fixture
def correction_case(monkeypatch):
    # No live data: the default suite uses a disposable SQLite database.
    from app.db import get_conn
    pid, cid = gen_id(), gen_id()
    projects.create_project({'id': pid, 'name': 'Correction regression',
        'style_config': '{}', 'created_at': '2026-09-13', 'updated_at': '2026-09-13'})
    chapter = chapters.upsert_chapter(cid, pid, {'title': '第1章',
        'content': '枕头换成了白菜。早餐牛奶煮挂面，出锅撒葱花。'}, '2026-09-13')
    outline = {'outlineVersion': 4, 'nodes': [
        {'id': 'v-correction', 'type': 'volume', 'title': '第一卷'},
        {'id': 'arc-cabbage', 'type': 'arc', 'parentId': 'v-correction', 'order': 1,
         'title': '白菜枕头', 'summary': '梧渡妈妈买白菜当枕头，梧渡睡得格外香，拍视频后没再深究。'},
        {'id': 'arc-noodles', 'type': 'arc', 'parentId': 'v-correction', 'order': 2,
         'title': '牛奶煮挂面', 'summary': '早餐店用牛奶煮挂面，梧渡吃了两碗发动态推荐。'},
        {'id': cid, 'type': 'chapter', 'parentId': 'v-correction', 'arcId': 'arc-cabbage',
         'title': '第1章', 'summary': '异变之芽·白菜与牛奶面', 'order': 1},
    ]}
    projects.update_project_fields(pid, {'outline': json.dumps(outline, ensure_ascii=False)}, '2026-09-13')
    provider = type('CaptureProvider', (), {'provider': 'mock', 'model': 'mock'})()
    provider.chat = AsyncMock(return_value='梧渡躺在白菜上，叶子微凉。他翻个身，安心睡去。')
    monkeypatch.setattr(ai_service, 'get_runtime_provider', lambda _: provider)
    yield pid, cid, chapter['content'], provider
    with get_conn() as conn:
        conn.execute('DELETE FROM chapter_rewrites WHERE project_id=?', (pid,))
    projects.delete_project(pid)


@pytest.mark.asyncio
async def test_actual_adjustment_does_not_send_rejected_plot_or_stale_chapter_summary(correction_case):
    pid, cid, original, provider = correction_case
    result = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, instruction='调整第1章正文，不要出现其他弧的内容！')
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert '白菜枕头' in sent
    assert '牛奶' not in sent
    assert '只改善表达' not in sent
    assert result['original'] == original and result['diff']
    assert chapters.get_chapter(cid, pid)['content'] == original
    # Latest applied outline is never rewritten as a side effect of editing prose.
    assert projects.get_project(pid)['outline']['nodes'][-1]['summary'] == '异变之芽·白菜与牛奶面'
    service.accept_candidate(result['id'])
    assert chapters.get_chapter(cid, pid)['content'] == result['candidate']
    assert provider.chat.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('instruction', [
    '重写本章正文的措辞，剧情不要变',
    '重写本章正文，只改措辞',
    '只润色正文，不要删除其他弧内容',
    '润色正文，不要新增其他弧情节',
])
async def test_wording_only_rewrite_preserves_source_and_chapter_goal(correction_case, instruction):
    pid, cid, original, provider = correction_case
    await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, instruction=instruction)
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert original in sent
    assert '只改善表达' in sent


@pytest.mark.asyncio
async def test_chapter_synopsis_edit_never_receives_prose_replacement_contract(correction_case):
    pid, cid, _, provider = correction_case
    await ai_service.chat_reply(ChatRequest(projectId=pid, chapterId=cid, messages=[
        {'role': 'user', 'content': '按照新大纲修改第1章摘要'},
    ]))
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert '只返回可应用的 JSON 补丁' in sent
    assert '完整替换正文' not in sent
    assert '当前 Copilot 动作' not in sent


@pytest.mark.asyncio
async def test_outline_alignment_changes_plot_contract_but_keeps_author_chapter_goal(correction_case):
    pid, cid, original, provider = correction_case
    outline = projects.get_project(pid)['outline']
    outline['nodes'][-1]['summary'] = '先拍视频，然后才试睡'
    projects.update_project_fields(pid, {'outline': json.dumps(outline, ensure_ascii=False)}, '2026-09-13')
    await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, instruction='按照新大纲调整正文')
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert '先拍视频，然后才试睡' in sent
    assert '只改善表达' not in sent


@pytest.mark.asyncio
async def test_selected_plot_correction_only_replaces_selection(correction_case):
    pid, cid, original, provider = correction_case
    selection = '早餐牛奶煮挂面，出锅撒葱花。'
    provider.chat.return_value = '他抱着白菜枕头，又睡了片刻。'
    candidate = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, selection_text=selection,
        instruction='调整这段正文，删除其他弧的内容')
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert selection in sent
    assert '只改善表达' not in sent
    assert '从头生成本章' not in sent
    assert candidate['original'] == selection
    service.accept_candidate(candidate['id'])
    assert chapters.get_chapter(cid, pid)['content'] == '枕头换成了白菜。他抱着白菜枕头，又睡了片刻。'


@pytest.mark.asyncio
async def test_manual_regeneration_retains_whole_chapter_correction_scope(correction_case):
    pid, cid, original, provider = correction_case
    first = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, instruction='调整第1章正文，不要出现其他弧的内容！')
    provider.chat.reset_mock()
    second = await service.regenerate_candidate(first['id'])
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert '牛奶' not in sent
    assert second['original'] == original
    assert second['id'] != first['id']
    assert chapters.get_chapter(cid, pid)['content'] == original
    assert provider.chat.await_count == 1


@pytest.mark.asyncio
async def test_whole_chapter_regeneration_cannot_silently_shrink_to_selection(correction_case):
    pid, cid, original, provider = correction_case
    candidate = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, instruction='调整第1章正文，不要出现其他弧的内容！')
    chapters.upsert_chapter(cid, pid, {'content': original + '后来补写的一段。'}, '2026-09-13')
    provider.chat.reset_mock()
    with pytest.raises(ValueError, match='原文已经变化'):
        await service.regenerate_candidate(candidate['id'])
    assert provider.chat.await_count == 0


@pytest.mark.asyncio
async def test_identical_reply_is_not_presented_as_successful_edit(correction_case):
    pid, cid, original, provider = correction_case
    provider.chat.return_value = original
    with pytest.raises(ValueError, match='未产生.*修改'):
        await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
            current_text=original, instruction='调整第1章正文，不要出现其他弧的内容！')
    assert chapter_rewrites.list_rewrites(pid, chapter_id=cid) == []
    assert chapters.get_chapter(cid, pid)['content'] == original
    assert provider.chat.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['failed', 'fallback'])
async def test_failed_provider_text_never_becomes_prose_candidate(correction_case, monkeypatch, mode):
    pid, cid, original, _ = correction_case
    monkeypatch.setattr(ai_service, 'chat_reply', AsyncMock(return_value={
        'mode': mode, 'reply': '模型不可用', 'error_code': 'response_invalid'}))
    with pytest.raises(ValueError):
        await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite', current_text=original)
    assert chapter_rewrites.list_rewrites(pid, chapter_id=cid) == []
    assert chapters.get_chapter(cid, pid)['content'] == original


@pytest.mark.asyncio
async def test_no_change_diagnostic_is_persisted_without_prose(correction_case, monkeypatch):
    from app.llm.audit import record_llm_call
    from app.db import get_conn
    pid, cid, original, _ = correction_case
    trace = record_llm_call(project_id=pid, provider='mock', model='mock', source='test', status='success')
    monkeypatch.setattr(ai_service, 'chat_reply', AsyncMock(return_value={
        'mode': 'llm', 'reply': original, 'trace_id': trace}))
    with pytest.raises(ValueError, match=trace):
        await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite', current_text=original)
    with get_conn() as conn:
        row = conn.execute('SELECT postprocess_status,postprocess_stage,postprocess_code,response_shape FROM llm_audit_log WHERE id=?', (trace,)).fetchone()
    assert tuple(row) == ('failed', 'candidate_create', 'copilot_no_change', 'prose')


@pytest.mark.asyncio
async def test_select_all_without_current_text_still_uses_whole_chapter_scope(correction_case):
    pid, cid, original, provider = correction_case
    candidate = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        selection_text=original, instruction='调整第1章正文，不要出现其他弧的内容！')
    sent = '\n'.join(m['content'] for m in provider.chat.await_args.args[0])
    assert '牛奶' not in sent
    assert candidate['original'] == original


@pytest.mark.asyncio
async def test_stale_whole_chapter_is_rejected_before_model_call(correction_case):
    pid, cid, original, provider = correction_case
    chapters.upsert_chapter(cid, pid, {'content': '已经接受的新版正文。'}, '2026-09-13')
    with pytest.raises(ValueError, match='正文.*(更新|保存)'):
        await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
            current_text=original, instruction='继续调整正文')
    assert provider.chat.await_count == 0
    assert chapter_rewrites.list_rewrites(pid, chapter_id=cid) == []


@pytest.mark.asyncio
async def test_accept_returns_saved_content_for_immediate_display_and_next_edit(correction_case):
    pid, cid, original, provider = correction_case
    first = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=original, instruction='调整正文')
    accepted = service.accept_candidate(first['id'])
    assert accepted['applied_content'] == first['candidate']
    provider.chat.return_value = '第二次调整后的完整正文。'
    second = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite',
        current_text=accepted['applied_content'], instruction='继续调整正文')
    again = service.accept_candidate(second['id'])
    assert chapters.get_chapter(cid, pid)['content'] == again['applied_content']
    assert again['applied_content'] == '第二次调整后的完整正文。'


@pytest.mark.asyncio
async def test_stale_accept_preserves_newer_prose_and_returns_actionable_chinese(correction_case):
    pid, cid, original, _ = correction_case
    candidate = await service.create_candidate(project_id=pid, chapter_id=cid, action='rewrite', current_text=original)
    chapters.upsert_chapter(cid, pid, {'content': '作者后来保存的新正文。'}, '2026-09-13')
    with pytest.raises(ValueError, match='正文.*更新'):
        service.accept_candidate(candidate['id'])
    assert chapters.get_chapter(cid, pid)['content'] == '作者后来保存的新正文。'
    assert chapter_rewrites.get_rewrite(candidate['id'])['status'] == 'proposed'
