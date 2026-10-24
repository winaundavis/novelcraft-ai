import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from app.models.schemas import ChatRequest, ContinueWritingRequest, GenerateCharacterRequest
from app.services import ai_service


@pytest.mark.asyncio
async def test_rewrite_reads_latest_outline_outside_clipped_context():
    request = ChatRequest(projectId='p1', chapterId='c1', action='rewrite',
                          messages=[{'role': 'user', 'content': '按照新大纲调整正文'}])
    assembled = {'profile': 'scene_writing', 'text': '裁剪后的旧正文', 'chars': 8, 'budget': 5000, 'blocks': []}
    project = {'outline': {'outlineVersion': 2, 'nodes': [
        {'id': 'v1', 'type': 'volume', 'title': '第一卷', 'summary': '卷末才觉醒秘密能力'},
        {'id': 'a1', 'type': 'arc', 'parentId': 'v1', 'title': '初遇', 'summary': '只观察异常'},
        {'id': 'c1', 'type': 'chapter', 'parentId': 'a1', 'title': '门口', 'summary': '发现信封'},
        {'id': 'a2', 'type': 'arc', 'parentId': 'v1', 'summary': '未来秘密不可提前出现'},
    ]}}
    with (
        patch.object(ai_service.project_context_service, 'assemble_project_context', return_value=assembled),
        patch.object(ai_service.projects_dao, 'get_project', return_value=project),
        patch.object(ai_service.llm, 'chat', new=AsyncMock(return_value='候选正文')) as chat,
    ):
        await ai_service.chat_reply(request)
        sent = '\n'.join(m['content'] for m in chat.await_args.args[0] if m['role'] == 'system')
        assert '发现信封' in sent and '只观察异常' in sent
        assert '未来秘密不可提前出现' not in sent
        assert '卷末才觉醒秘密能力' not in sent
        assert '暗示' in sent
        project['outline']['nodes'][2]['summary'] = '改为收到空包裹'
        await ai_service.chat_reply(request)
        sent = '\n'.join(m['content'] for m in chat.await_args.args[0] if m['role'] == 'system')
        assert '改为收到空包裹' in sent and '发现信封' not in sent
        assert '旧正文' in sent and '只返回' in sent


@pytest.mark.asyncio
async def test_outline_intent_uses_latest_user_message_not_old_prose_request():
    request = ChatRequest(projectId='p1', messages=[
        {'role': 'user', 'content': '请调整大纲，让第一弧只写日常'},
        {'role': 'assistant', 'content': '好的，我会调整大纲。'},
        {'role': 'user', 'content': '现在按这个方向修改正文，不要出现后续弧内容'},
    ])
    assembled = {'profile': 'scene_writing', 'text': '最小上下文', 'chars': 6, 'budget': 5000, 'blocks': []}
    with (
        patch.object(ai_service.project_context_service, 'assemble_project_context', return_value=assembled),
        patch.object(ai_service.llm, 'chat', new=AsyncMock(return_value='正文建议')) as chat,
    ):
        result = await ai_service.chat_reply(request)
    assert result['intent'] == 'consultation'
    sent = '\n'.join(m['content'] for m in chat.await_args.args[0])
    assert '只返回可应用的 JSON 补丁' not in sent


@pytest.mark.asyncio
async def test_outline_request_does_not_fail_when_outline_system_is_large():
    request = ChatRequest(projectId='p1', messages=[{'role': 'user', 'content': '调整第一卷剧情弧'}])
    assembled = {'profile': 'plot_planning', 'text': 'x' * 4800, 'chars': 4800, 'budget': 5000, 'blocks': []}
    large_outline = {'outline': {'nodes': [
        {'id': 'v1', 'type': 'volume', 'title': '卷', 'summary': '卷' * 100},
        *({'id': f'a{i}', 'type': 'arc', 'parentId': 'v1', 'title': f'弧{i}', 'summary': '弧情节' * 100} for i in range(80)),
    ]}}
    with (
        patch.object(ai_service.project_context_service, 'assemble_project_context', return_value=assembled),
        patch.object(ai_service.projects_dao, 'get_project', return_value=large_outline),
        patch.object(ai_service.chapters_dao, 'list_chapters', return_value=[]),
        patch.object(ai_service.llm, 'chat', new=AsyncMock(return_value='{"patchMode":"merge","upsertNodes":[],"removeNodeIds":[]}')) as chat,
    ):
        result = await ai_service.chat_reply(request)
    assert result['mode'] == 'llm'
    assert sum(len(m['content']) for m in chat.await_args.args[0]) <= ai_service.OUTLINE_EDIT_REQUEST_MAX_CHARS


@pytest.mark.asyncio
async def test_chat_reply_injects_project_context_before_messages():
    request = ChatRequest(
        projectId="p1",
        chapterId="c2",
        currentText="未保存正文",
        selectionText="当前选区",
        contextProfile="consistency_check",
        messages=[{"role": "user", "content": "这里是否矛盾？"}],
    )
    assembled = {
        "profile": "consistency_check",
        "text": "【项目上下文】\nL1 永久事实",
        "chars": 20,
        "budget": 5000,
        "blocks": [{"key": "facts", "layer": "L1", "chars": 8, "sourceIds": ["f1"]}],
    }

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled) as assemble,
        patch.object(ai_service.llm, "chat", new=AsyncMock(return_value="没有矛盾。")) as chat,
    ):
        result = await ai_service.chat_reply(request)

    assemble.assert_called_once_with(
        project_id="p1",
        chapter_id="c2",
        current_text="未保存正文",
        selection_text="当前选区",
        query="这里是否矛盾？",
        profile="consistency_check",
    )
    messages = chat.await_args.args[0]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "system", "content": assembled["text"]}
    assert messages[2] == {"role": "user", "content": "这里是否矛盾？"}
    assert result["reply"] == "没有矛盾。"
    assert result["context"]["profile"] == "consistency_check"
    assert result["context"]["blocks"][0]["sourceIds"] == ["f1"]


@pytest.mark.asyncio
async def test_chat_reply_keeps_legacy_request_compatible():
    request = ChatRequest(projectId="p1", messages=[{"role": "user", "content": "给点建议"}])
    assembled = {"profile": "scene_writing", "text": "项目资料", "chars": 4, "budget": 5000, "blocks": []}

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled) as assemble,
        patch.object(ai_service.llm, "chat", new=AsyncMock(return_value="建议")),
    ):
        await ai_service.chat_reply(request)

    assert assemble.call_args.kwargs["chapter_id"] == ""
    assert assemble.call_args.kwargs["current_text"] == ""
    assert assemble.call_args.kwargs["selection_text"] == ""
    assert assemble.call_args.kwargs["profile"] is None


@pytest.mark.asyncio
async def test_outline_chat_bounds_old_history_but_keeps_latest_instruction():
    latest_instruction = "请根据我们已确定的方向优化大纲，重点调整第二卷的两个剧情弧"
    request = ChatRequest(
        projectId="p1",
        messages=[
            {"role": "user", "content": "最早的讨论-" + "甲" * 7000},
            {"role": "assistant", "content": "旧模型长回复-" + "乙" * 12000},
            {"role": "user", "content": "近期约定-" + "丙" * 5000},
            {"role": "assistant", "content": "近期答复-" + "丁" * 8000},
            {"role": "user", "content": latest_instruction},
        ],
    )
    assembled = {"profile": "plot_planning", "text": "项目资料", "chars": 4, "budget": 5000, "blocks": []}

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled),
        patch.object(ai_service.projects_dao, "get_project", return_value={"outline": {"mainSummary": "主线", "nodes": []}}),
        patch.object(ai_service.llm, "chat", new=AsyncMock(return_value='{"patchMode":"merge","upsertNodes":[],"removeNodeIds":[]}')) as chat,
    ):
        await ai_service.chat_reply(request)

    messages = chat.await_args.args[0]
    conversational = [message for message in messages if message["role"] in {"user", "assistant"}]
    assert conversational[-1] == {"role": "user", "content": latest_instruction}
    assert sum(len(message["content"]) for message in conversational) <= 7000
    assert all("最早的讨论" not in message["content"] for message in conversational)
    assert any("近期约定" in message["content"] for message in conversational)


@pytest.mark.asyncio
async def test_large_outline_chat_request_stays_under_hard_limit():
    nodes = []
    chapter_order = 1
    for volume_order in range(1, 4):
        volume_id = f"v-{volume_order}"
        nodes.append({"id": volume_id, "type": "volume", "title": f"第{volume_order}卷", "summary": "卷剧情" * 40, "order": volume_order})
        for arc_order in range(1, 4):
            arc_id = f"a-{volume_order}-{arc_order}"
            nodes.append({"id": arc_id, "type": "arc", "parentId": volume_id, "title": f"第{arc_order}弧", "summary": "弧剧情" * 40, "order": arc_order})
            for _ in range(22):
                nodes.append({"id": f"ch-{chapter_order}", "type": "chapter", "parentId": volume_id, "arcId": arc_id, "title": f"第{chapter_order}章", "order": chapter_order})
                chapter_order += 1
    request = ChatRequest(projectId="p-large", messages=[{"role": "user", "content": "请优化大纲中的各卷剧情弧"}])
    assembled = {"profile": "plot_planning", "text": "项目资料" * 200, "chars": 800, "budget": 5000, "blocks": []}

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled),
        patch.object(ai_service.projects_dao, "get_project", return_value={"outline": {"mainSummary": "主线", "nodes": nodes}}),
        patch.object(ai_service, "get_runtime_provider", return_value=ai_service.llm),
        patch.object(ai_service.llm, "chat", new=AsyncMock(return_value='{"patchMode":"merge","upsertNodes":[],"removeNodeIds":[]}')) as chat,
    ):
        await ai_service.chat_reply(request)

    messages = chat.await_args.args[0]
    assert sum(len(str(message.get("content") or "")) for message in messages) <= 14000
    prompt = "\n".join(message["content"] for message in messages)
    assert "a-3-3" in prompt
    assert "ch-150" not in prompt


@pytest.mark.asyncio
async def test_whole_book_chapter_reorder_is_blocked_before_provider_call():
    request = ChatRequest(projectId="p1", messages=[{"role": "user", "content": "请重排全部章节并重新安排所有章节"}])
    assembled = {"profile": "plot_planning", "text": "项目资料", "chars": 4, "budget": 5000, "blocks": []}
    provider_call = AsyncMock()

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled),
        patch.object(ai_service.projects_dao, "get_project", return_value={"outline": {"mainSummary": "主线", "nodes": []}}),
        patch.object(ai_service, "get_runtime_provider", return_value=ai_service.llm),
        patch.object(ai_service.llm, "chat", new=provider_call),
    ):
        result = await ai_service.chat_reply(request)

    assert result["mode"] == "failed"
    assert result["error_code"] == "outline_scope_too_broad"
    provider_call.assert_not_awaited()


def test_chat_api_rejects_missing_project_before_context_or_llm():
    with patch("app.api.ai.chat_reply", new=AsyncMock(return_value={"reply": "不应调用"})) as chat:
        with TestClient(app) as client:
            response = client.post(
                "/api/ai/chat",
                json={"projectId": "missing-project", "messages": [{"role": "user", "content": "测试"}]},
            )

    assert response.status_code == 404
    chat.assert_not_called()


@pytest.mark.asyncio
async def test_generate_character_uses_project_planning_context():
    request = GenerateCharacterRequest(projectId="p1", role="supporting", hints="设计守门人", genre="mystery")
    assembled = {"profile": "plot_planning", "text": "项目长期剧情", "chars": 6, "budget": 4400, "blocks": []}

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled) as assemble,
        patch.object(ai_service.llm, "chat", new=AsyncMock(return_value="足够长的人物设定" * 20)) as chat,
    ):
        await ai_service.generate_character(request)

    assert assemble.call_args.kwargs == {
        "project_id": "p1",
        "query": "设计守门人",
        "profile": "plot_planning",
        "include_current_text": False,
    }
    messages = chat.await_args.args[0]
    assert messages[1] == {"role": "system", "content": "项目长期剧情"}


@pytest.mark.asyncio
async def test_editor_streaming_continuation_adds_project_context_without_duplicate_current_text():
    request = ContinueWritingRequest(
        projectId="p1",
        chapterId="c2",
        contextBefore="编辑器已经携带的未保存正文",
        direction="继续追查钥匙",
    )
    assembled = {"profile": "continuation", "text": "L1-L3 与章节元数据", "chars": 12, "budget": 5400, "blocks": []}
    captured: dict = {}

    async def fake_stream(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        yield "续写片段"

    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled) as assemble,
        patch.object(ai_service.llm, "chat_stream", new=fake_stream),
    ):
        events = [event async for event in ai_service.continue_writing_stream(request)]

    assert assemble.call_args.kwargs == {
        "project_id": "p1",
        "chapter_id": "c2",
        "query": "继续追查钥匙",
        "profile": "continuation",
        "include_current_text": False,
    }
    assert captured["messages"][1] == {"role": "system", "content": assembled["text"]}
    assert sum("编辑器已经携带的未保存正文" in item["content"] for item in captured["messages"]) == 1
    payloads = [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event.startswith("data: {")
    ]
    assert any(payload.get("chunk") == "续写片段" for payload in payloads)
