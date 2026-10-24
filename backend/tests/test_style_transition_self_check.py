"""Style-plan freshness checks around the self-check LLM boundary."""
from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from main import app, _request_windows
from app.services import fact_check_service, hook_service, review_service
from app.services import novel_generation_service as ngs
from app.services.style_engine import transition_service


@pytest.fixture
def workspace():
    _request_windows.clear()
    with TestClient(app) as client:
        project_id = client.post(
            "/api/projects/", json={"name": "self-check-transition", "genre": "xuanhuan"}
        ).json()["data"]["id"]
        yield client, project_id


def _start_plan(client: TestClient, project_id: str) -> dict:
    response = client.put(
        f"/api/projects/{project_id}/style-transition",
        json={
            "pace": "fast",
            "targetStyle": {
                "presetKey": "emotional_resonance",
                "customPrompt": "保留克制对话",
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _job(project_id: str) -> ngs.NovelGenerationJob:
    return ngs.NovelGenerationJob(
        id="style-self-check-job",
        project_id=project_id,
        mode="all",
        requested_chapter_id=None,
        status="running",
        message="",
        target_words=0,
        total_chapters=1,
    )


async def _self_check(job: ngs.NovelGenerationJob, result: dict) -> dict:
    return await ngs._run_self_check_loop(
        job=job,
        chapter={"id": "style-self-check-chapter", "title": "第一章", "order": 1},
        result=result,
        memory_context=None,
        previous_id=None,
        next_id=None,
        hook_instruction="",
        apply_deterministic_gate=False,
    )


@pytest.mark.asyncio
async def test_stale_style_token_is_rejected_before_modern_review_llm(workspace):
    client, project_id = workspace
    stale = _start_plan(client, project_id)
    _start_plan(client, project_id)
    review_logic = AsyncMock(return_value={"issues": []})

    with patch.object(review_service, "review_logic", review_logic), patch.object(
        review_service, "review_language", new=AsyncMock(return_value={"issues": []})
    ):
        with pytest.raises(transition_service.TransitionRevisionConflict):
            await _self_check(
                _job(project_id),
                {
                    "content": "候选正文",
                    "chapter_contract": {"goal": "测试"},
                    "style_plan_id": stale["planId"],
                    "style_plan_revision": stale["revision"],
                },
            )

    review_logic.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_change_after_first_review_blocks_second_modern_review_llm(workspace):
    client, project_id = workspace
    current = _start_plan(client, project_id)
    model_calls = {"logic": 0, "language": 0}

    async def first_review(content, chapter_id, *, allow_llm_call):
        assert allow_llm_call("review", 900)
        model_calls["logic"] += 1
        _start_plan(client, project_id)
        return {"issues": []}

    async def second_review(content, chapter_id, *, allow_llm_call):
        assert allow_llm_call("review", 900)
        model_calls["language"] += 1
        return {"issues": []}

    with patch.object(review_service, "review_logic", new=first_review), patch.object(
        review_service, "review_language", new=second_review
    ):
        with pytest.raises(transition_service.TransitionRevisionConflict):
            await _self_check(
                _job(project_id),
                {
                    "content": "候选正文",
                    "chapter_contract": {"goal": "测试"},
                    "style_plan_id": current["planId"],
                    "style_plan_revision": current["revision"],
                },
            )

    assert model_calls == {"logic": 1, "language": 0}


@pytest.mark.asyncio
async def test_legacy_self_check_callback_without_style_token_remains_allowed(workspace):
    _, project_id = workspace
    validator = Mock()
    model_calls = Mock()

    async def fact_check(*, allow_llm_call, **kwargs):
        assert allow_llm_call("fact_check", 800)
        model_calls()
        return {"content": None, "stats": {}}

    with patch.object(transition_service, "validate_generation_token", validator), patch.object(
        fact_check_service, "run_fact_check_loop", new=fact_check
    ), patch.object(
        hook_service, "check_hook", new=AsyncMock(return_value={"has_hook": True})
    ), patch.object(hook_service, "record_hook", new=Mock()), patch.object(
        ngs.rewrite_service, "run_style_rewrite_chain", new=AsyncMock(return_value={})
    ):
        result = await _self_check(_job(project_id), {"content": "旧版候选正文"})

    assert result["content"] == "旧版候选正文"
    assert model_calls.call_count == 1
    validator.assert_not_called()
