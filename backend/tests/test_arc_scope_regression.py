"""Regeneration must not reintroduce later-arc plot through hidden inputs."""

from unittest.mock import AsyncMock

import pytest

from app.services import ai_service, chapter_contract_service as contracts
from app.services import copilot_candidate_service as candidates
from app.services import novel_generation_service as ngs


@pytest.fixture
def scoped_project(monkeypatch):
    project = {
        "id": "scope-project", "name": "当前弧隔离", "premise": "FUTURE_PREMISE",
        "characters": [{"id": "hero", "name": "梧渡", "role": "protagonist",
                        "basicInfo": {"identity": "学生"}, "arc": "FUTURE_CHARACTER_ARC",
                        "deepDesire": "FUTURE_CHARACTER_DESIRE"}],
        "worldRules": [],
        "outline": {"mainSummary": "FUTURE_MAIN_OUTLINE", "nodes": [
            {"id": "v1", "type": "volume", "title": "第一卷", "order": 1},
            {"id": "a1", "type": "arc", "parentId": "v1", "order": 1,
             "title": "白菜枕头", "summary": "梧渡醒来发现枕头是白菜，吃过早饭照常上学。"},
            {"id": "a2", "type": "arc", "parentId": "v1", "order": 2,
             "title": "FUTURE_ARC", "summary": "FUTURE_ARC_SUMMARY"},
            {"id": "c1", "type": "chapter", "parentId": "v1", "arcId": "a1",
             "title": "第一章", "summary": "FUTURE_OLD_CHAPTER_PLAN", "order": 1, "targetWordCount": 800},
            {"id": "c2", "type": "chapter", "parentId": "v1", "arcId": "a1", "title": "第二章", "order": 2},
            {"id": "c3", "type": "chapter", "parentId": "v1", "arcId": "a2", "title": "第三章", "order": 3},
        ]},
    }
    monkeypatch.setattr(contracts.projects_dao, "get_project", lambda _: project)
    monkeypatch.setattr(contracts.chapters_dao, "get_chapter", lambda cid, pid: {
        "id": cid, "project_id": pid, "title": "第一章", "content": "FUTURE_OLD_PROSE",
    })
    state = {"version": 9, "source": "author", "relationship_changes": ["FUTURE_RELATIONSHIP"],
             "last_major_state_change": "FUTURE_STATE", "current_phase_goal": "FUTURE_PHASE"}
    monkeypatch.setattr(contracts.story_state_service, "ensure_story_state", lambda _: state)
    monkeypatch.setattr(contracts.story_state_service, "get_story_state", lambda _: state)
    monkeypatch.setattr(contracts.chapter_contracts_dao, "save_contract", lambda p, c, value: value)
    monkeypatch.setattr(contracts.foreshadows_dao, "list_active_foreshadows", lambda *a, **k: [
        {"name": "FUTURE_UNBOUND_FORESHADOW"},
        {"name": "FUTURE_PLANTED_FORESHADOW", "plant_chapter_id": "c3"},
    ])
    monkeypatch.setattr(ngs.writing_experience_service, "render_accepted", lambda _: "FUTURE_EXPERIENCE")
    return project


@pytest.mark.asyncio
async def test_final_generation_request_excludes_global_future_and_old_prose(scoped_project, monkeypatch):
    chapter = scoped_project["outline"]["nodes"][3]
    monkeypatch.setattr(ngs, "_load_existing_chapter_text", lambda *a: "FUTURE_OLD_PROSE")
    monkeypatch.setattr(ngs, "_minimum_acceptable_words", lambda *a: 1)
    call = AsyncMock(return_value={"text": "甲" * 800, "source": "llm", "provider": "mock", "model": "mock"})
    monkeypatch.setattr(ngs, "generate_continuation_result", call)
    job = ngs.NovelGenerationJob(id="scope-job", project_id="scope-project", mode="single",
                               requested_chapter_id="c1", status="running", message="", target_words=800, total_chapters=1)
    result = await ngs._generate_chapter_with_expansion(
        job=job, chapter=chapter, chapter_index=0, total_chapters=1,
        premise=scoped_project["premise"], outline_summary=scoped_project["outline"]["mainSummary"],
        previous_summary="FUTURE_PREVIOUS_SUMMARY", characters=scoped_project["characters"], world_rules=[],
        hook_instruction="FUTURE_OPEN_HOOK",
        memory_context={"volume_summaries": ["FUTURE_VOLUME"], "timeline": "FUTURE_TIMELINE",
                        "entity_state": "FUTURE_ENTITY", "active_foreshadows": "FUTURE_MEMORY_FORESHADOW"},
    )
    sent = ai_service._build_continue_prompt(call.await_args.args[0])
    assert "FUTURE_" not in sent
    assert "白菜" in sent and "梧渡" in sent
    assert result["content"] == "甲" * 800
    assert call.await_count == 1


def test_arc_parent_shape_resolves_without_global_fallback(scoped_project):
    chapter = scoped_project["outline"]["nodes"][3]
    chapter.pop("arcId")
    chapter["parentId"] = "a1"
    scope = contracts._outline_scope(scoped_project, chapter, "c1")
    assert scope["arc_title"] == "白菜枕头"
    assert scope["chapter_count"] == 2


def test_dangling_arc_cannot_silently_fall_back_to_global_story(scoped_project):
    chapter = scoped_project["outline"]["nodes"][3]
    chapter["arcId"] = "missing-arc"
    with pytest.raises(ValueError, match="剧情弧"):
        contracts.compile_chapter_contract("scope-project", "c1")


@pytest.mark.asyncio
@pytest.mark.parametrize('instruction', [
    '按当前大纲重新生成第一章正文，不要出现其他弧的内容',
    '调整第1章正文，不要出现其他弧的内容！',
    '继续调整第1章正文，不要出现其他弧的内容！',
    '按当前弧纠正本章正文，删除第二弧的剧情',
])
async def test_outline_based_copilot_regeneration_keeps_original_only_for_diff(scoped_project, monkeypatch, instruction):
    monkeypatch.setattr(candidates.projects_dao, "project_exists", lambda _: True)
    monkeypatch.setattr(ai_service.project_context_service.chapters_dao, "list_chapters_global_order", lambda _: [
        {"id": "c1", "content": "FUTURE_OLD_PROSE", "order": 1},
        {"id": "c3", "content": "FUTURE_CHAPTER_THREE", "order": 3},
    ])
    provider = type("Provider", (), {"provider": "mock", "model": "mock"})()
    provider.chat = AsyncMock(return_value="梧渡拿起白菜枕头，端端正正放回床边。")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: provider)
    saved = {}
    def save_rewrite(p, c, action, **fields):
        saved.update(id="rewrite-scope", project_id=p, chapter_id=c, status="proposed", **fields)
        return "rewrite-scope"
    monkeypatch.setattr(candidates.rewrites_dao, "insert_rewrite", save_rewrite)
    monkeypatch.setattr(candidates.rewrites_dao, "get_rewrite", lambda _: saved)
    result = await candidates.create_candidate(
        project_id="scope-project", chapter_id="c1", action="rewrite", current_text="FUTURE_OLD_PROSE",
        instruction=instruction,
    )
    sent = "\n".join(m["content"] for m in provider.chat.await_args.args[0])
    assert "FUTURE_" not in sent
    assert "白菜枕头" in sent
    assert '只改善表达' not in sent
    assert result["original"] == "FUTURE_OLD_PROSE"
    assert result["status"] == "proposed"
    assert result["diff"]


@pytest.mark.asyncio
@pytest.mark.parametrize('instruction', [
    '请按最新大纲重新生成第一章正文',
    '调整第1章正文，不要出现其他弧的内容！',
])
async def test_prose_request_without_action_does_not_replay_old_outline_discussion(scoped_project, monkeypatch, instruction):
    from app.models.schemas import ChatRequest

    monkeypatch.setattr(ai_service.project_context_service.chapters_dao, "list_chapters_global_order", lambda _: [])
    provider = type("Provider", (), {"provider": "mock", "model": "mock"})()
    provider.chat = AsyncMock(return_value="依据当前剧情弧调整。")
    monkeypatch.setattr(ai_service, "get_runtime_provider", lambda _: provider)
    await ai_service.chat_reply(ChatRequest(projectId="scope-project", chapterId="c1", messages=[
        {"role": "user", "content": "请调整大纲，FUTURE_DISCUSSION"},
        {"role": "assistant", "content": "FUTURE_ASSISTANT_PROPOSAL"},
        {"role": "user", "content": instruction},
    ]))
    sent = "\n".join(m["content"] for m in provider.chat.await_args.args[0])
    assert "FUTURE_" not in sent
    assert "白菜枕头" in sent


def test_nested_arc_chapters_keep_global_volume_order(scoped_project):
    outline = {"nodes": [
        {"id": "v1", "type": "volume"}, {"id": "v2", "type": "volume"},
        {"id": "a1", "type": "arc", "parentId": "v1"},
        {"id": "a2", "type": "arc", "parentId": "v2"},
        {"id": "c1", "type": "chapter", "parentId": "a1", "order": 2},
        {"id": "c2", "type": "chapter", "parentId": "a2", "order": 1},
    ]}
    chapters = ngs._chapter_nodes_from_outline(outline)
    assert [node["id"] for node in chapters] == ["c1", "c2"]
    assert chapters[0]["arcId"] == "a1"
    assert chapters[0]["parentId"] == "v1"
    assert outline["nodes"][4]["parentId"] == "a1"
