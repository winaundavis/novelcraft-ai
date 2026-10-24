from unittest.mock import AsyncMock, patch

import pytest

from app.models.schemas import ChatRequest
from app.services import ai_service, copilot_action_service


def test_all_copilot_actions_have_profile_and_instruction():
    assert len(copilot_action_service.COPILOT_ACTIONS) >= 12
    for action, spec in copilot_action_service.COPILOT_ACTIONS.items():
        assert action == spec["action"]
        assert spec["profile"] in {"plot_planning", "scene_writing", "continuation", "consistency_check"}
        assert spec["instruction"]


@pytest.mark.asyncio
async def test_chat_action_adds_deterministic_task_contract_and_returns_action():
    request = ChatRequest(
        projectId="p1",
        chapterId="c2",
        action="analyze_next",
        messages=[{"role": "user", "content": "给我下一步方案"}],
    )
    assembled = {"profile": "plot_planning", "text": "项目状态", "chars": 4, "budget": 100, "blocks": []}
    with (
        patch.object(ai_service.project_context_service, "assemble_project_context", return_value=assembled),
        patch.object(ai_service.llm, "chat", new=AsyncMock(return_value="方案 A/B/C")) as chat,
    ):
        result = await ai_service.chat_reply(request)

    assert result["action"] == "analyze_next"
    assert result["context"]["action"] == "analyze_next"
    assert chat.await_args.args[0][2]["role"] == "system"
    assert "下一章" in chat.await_args.args[0][2]["content"]
