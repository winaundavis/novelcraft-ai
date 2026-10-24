from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from app.dao import chapter_contracts_dao, story_states_dao
from app.services import chapter_contract_service, story_state_service


@pytest.fixture
def full_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE story_states (
            id TEXT PRIMARY KEY, project_id TEXT UNIQUE NOT NULL,
            current_volume_goal TEXT NOT NULL DEFAULT '', current_phase_goal TEXT NOT NULL DEFAULT '',
            protagonist_goal TEXT NOT NULL DEFAULT '', protagonist_obstacle TEXT NOT NULL DEFAULT '',
            main_conflict TEXT NOT NULL DEFAULT '', sub_conflicts_json TEXT NOT NULL DEFAULT '[]',
            relationship_changes_json TEXT NOT NULL DEFAULT '[]', current_mysteries_json TEXT NOT NULL DEFAULT '[]',
            active_foreshadows_json TEXT NOT NULL DEFAULT '[]', unresolved_questions_json TEXT NOT NULL DEFAULT '[]',
            promised_payoffs_json TEXT NOT NULL DEFAULT '[]', last_major_state_change TEXT NOT NULL DEFAULT '',
            next_stage_directions_json TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT 'seeded',
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE chapter_contracts (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
            version INTEGER NOT NULL, status TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
            conflict TEXT NOT NULL DEFAULT '', state_change TEXT NOT NULL DEFAULT '',
            characters_json TEXT NOT NULL DEFAULT '[]', key_events_json TEXT NOT NULL DEFAULT '[]',
            must_payoff_json TEXT NOT NULL DEFAULT '[]', must_avoid_json TEXT NOT NULL DEFAULT '[]',
            ending_hook TEXT NOT NULL DEFAULT '', pacing TEXT NOT NULL DEFAULT '',
            story_state_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(project_id, chapter_id, version)
        );
        """
    )
    monkeypatch.setattr(story_states_dao, "get_conn", lambda: conn)
    monkeypatch.setattr(chapter_contracts_dao, "get_conn", lambda: conn)
    monkeypatch.setattr(story_state_service.foreshadows_dao, "list_active_foreshadows", lambda *args, **kwargs: [])
    yield conn
    conn.close()


def _project(project_id: str = "p0b-project") -> dict:
    return {
        "id": project_id,
        "name": "测试长篇",
        "premise": "主角追查失踪的师父。",
        "genre": "xuanhuan",
        "characters": [
            {"name": "沈砚", "role": "protagonist", "surfaceMotivation": "寻找师父"},
            {"name": "林芜", "role": "supporting", "surfaceMotivation": "隐藏真相"},
        ],
        "outline": {
            "mainSummary": "追查失踪师父并揭开宗门旧案。",
            "nodes": [
                {
                    "id": "ch-2",
                    "title": "雨夜问碑",
                    "summary": "沈砚进入禁地寻找第二块碑。",
                    "conflict": "林芜阻止他进入禁地。",
                    "revelation": "石碑留下了师父的名字。",
                    "characters": ["沈砚", "林芜"],
                    "events": ["进入禁地", "发现碑文"],
                    "hook": "碑文在雨中自行改写。",
                    "pacing": "escalation",
                }
            ],
        },
    }


def _chapter(project_id: str = "p0b-project") -> dict:
    return {"id": "ch-2", "project_id": project_id, "title": "雨夜问碑", "chapter_order": 2}


def test_ensure_story_state_seeds_once_and_preserves_author_edit(full_memory_db):
    project = _project()
    with patch.object(story_state_service.projects_dao, "get_project", return_value=project):
        first = story_state_service.ensure_story_state(project["id"])
        assert first["current_phase_goal"] == "沈砚进入禁地寻找第二块碑。"

        story_states_dao.update_story_state(
            project["id"], {"current_phase_goal": "作者指定：先救出林芜。"}
        )
        second = story_state_service.ensure_story_state(project["id"])

    assert second["current_phase_goal"] == "作者指定：先救出林芜。"


def test_compile_contract_uses_outline_state_and_active_foreshadow(full_memory_db):
    project = _project()
    with (
        patch.object(chapter_contract_service.projects_dao, "get_project", return_value=project),
        patch.object(chapter_contract_service.chapters_dao, "get_chapter", return_value=_chapter()),
        patch.object(
            chapter_contract_service.foreshadows_dao,
            "list_active_foreshadows",
            return_value=[
                {
                    "id": "f-1",
                    "name": "碑文缺口",
                    "description": "缺口指向师父留下的暗号。",
                    "target_chapter_id": "ch-2",
                    "status": "progressing",
                }
            ],
        ),
    ):
        contract = chapter_contract_service.compile_chapter_contract(project["id"], "ch-2")

    assert contract["goal"] == "沈砚进入禁地寻找第二块碑。"
    assert contract["conflict"] == "林芜阻止他进入禁地。"
    assert "碑文缺口" in contract["must_payoff"]
    assert "碑文在雨中自行改写" in contract["ending_hook"]
    assert contract["story_state_version"] == 1


def test_contract_scopes_structured_outline_to_current_arc(full_memory_db):
    project = {
        **_project(),
        "outline": {
            "mainSummary": "第一弧吃白菜，第二弧才发生血雨并发现黑塔。",
            "nodes": [
                {"id": "v1", "type": "volume", "title": "第一卷", "summary": "从白菜日常推进到血雨黑塔。", "order": 1},
                {"id": "a1", "type": "arc", "parentId": "v1", "title": "白菜枕头", "summary": "梧渡只经历白菜枕头的古怪日常，并未察觉异常。", "order": 1},
                {"id": "a2", "type": "arc", "parentId": "v1", "title": "血雨黑塔", "summary": "血雨降临，梧渡发现黑塔。", "order": 2},
                {
                    "id": "ch-2", "type": "chapter", "parentId": "v1", "arcId": "a1",
                    "title": "第一章", "summary": "旧摘要错误写入血雨和黑塔。", "order": 1,
                },
            ],
        },
    }
    with (
        patch.object(chapter_contract_service.projects_dao, "get_project", return_value=project),
        patch.object(chapter_contract_service.chapters_dao, "get_chapter", return_value=_chapter()),
        patch.object(chapter_contract_service.story_state_service, "ensure_story_state", return_value={"version": 1}),
        patch.object(chapter_contract_service.foreshadows_dao, "list_active_foreshadows", return_value=[]),
        patch.object(chapter_contract_service.chapter_contracts_dao, "save_contract", side_effect=lambda _p, _c, value: value),
    ):
        contract = chapter_contract_service.compile_chapter_contract(project["id"], "ch-2")

    rendered = chapter_contract_service.render_contract(contract)
    scope = contract["arc_context"]["outline_scope"]
    assert contract["goal"] == "梧渡只经历白菜枕头的古怪日常，并未察觉异常。"
    assert scope["arc_title"] == "白菜枕头"
    assert scope["arc_summary"] in rendered
    assert scope["has_later_arcs"] is True
    assert "血雨" not in rendered
    assert "黑塔" not in rendered
    assert any("后续剧情弧" in item for item in contract["must_avoid"])


def test_contract_persists_canonical_character_ids_but_renders_display_names(full_memory_db):
    from app.services import chapter_contract_service

    project = {
        "id": "project-entity-contract",
        "characters": [
            {
                "id": "lin-qiao",
                "name": "Lin Qiao",
                "display_name": "林乔",
                "aliases": ["林乔", "Lin Qiao"],
                "role": "protagonist",
            }
        ],
        "outline": {"nodes": [{"id": "chapter-1", "characters": ["Lin Qiao"]}]},
    }
    chapter = {"id": "chapter-1", "project_id": project["id"]}
    state = {"version": 1, "current_phase_goal": "抵达桥头"}

    with (
        patch.object(chapter_contract_service.projects_dao, "get_project", return_value=project),
        patch.object(chapter_contract_service.chapters_dao, "get_chapter", return_value=chapter),
        patch.object(chapter_contract_service.story_state_service, "ensure_story_state", return_value=state),
        patch.object(chapter_contract_service.foreshadows_dao, "list_active_foreshadows", return_value=[]),
        patch.object(chapter_contract_service.commercial_pacing_service, "compile_pacing_context", return_value={"chapter_mode": "Action"}),
        patch.object(chapter_contract_service.chapter_contracts_dao, "save_contract", side_effect=lambda _p, _c, value: value),
    ):
        contract = chapter_contract_service.compile_chapter_contract(project["id"], chapter["id"])

    assert contract["characters"] == ["character_lin_qiao"]
    assert contract["arc_context"]["entity_catalog"] == [{"id": "character_lin_qiao", "display_name": "林乔", "aliases": ["林乔", "Lin Qiao"]}]
    assert "本章人物：林乔" in chapter_contract_service.render_contract(contract)
    assert "character_lin_qiao" not in chapter_contract_service.render_contract(contract)


def test_rendered_contract_is_bounded_and_marked_as_story_contract():
    rendered = chapter_contract_service.render_contract(
        {
            "goal": "推进主线",
            "conflict": "遭遇阻力",
            "state_change": "获得新线索",
            "characters": ["沈砚"],
            "key_events": ["进入禁地"],
            "must_payoff": ["碑文缺口"],
            "must_avoid": ["违背已确认事实"],
            "ending_hook": "留下问题",
            "pacing": "escalation",
        }
    )
    assert "【本章创作合同】" in rendered
    assert "本章目标：推进主线" in rendered
    assert len(rendered) <= chapter_contract_service.CONTRACT_RENDER_BUDGET


def test_recompile_reuses_version_when_sources_are_unchanged(full_memory_db):
    project = _project()
    with (
        patch.object(chapter_contract_service.projects_dao, "get_project", return_value=project),
        patch.object(chapter_contract_service.chapters_dao, "get_chapter", return_value=_chapter()),
        patch.object(chapter_contract_service.foreshadows_dao, "list_active_foreshadows", return_value=[]),
        patch.object(story_state_service.projects_dao, "get_project", return_value=project),
    ):
        first = chapter_contract_service.get_or_compile_chapter_contract("p0b-project", "ch-2")
        second = chapter_contract_service.get_or_compile_chapter_contract("p0b-project", "ch-2")

    assert first["version"] == second["version"] == 1


def test_story_state_version_change_creates_new_contract_version(full_memory_db):
    project = _project()
    with (
        patch.object(chapter_contract_service.projects_dao, "get_project", return_value=project),
        patch.object(chapter_contract_service.chapters_dao, "get_chapter", return_value=_chapter()),
        patch.object(chapter_contract_service.foreshadows_dao, "list_active_foreshadows", return_value=[]),
        patch.object(story_state_service.projects_dao, "get_project", return_value=project),
    ):
        first = chapter_contract_service.get_or_compile_chapter_contract("p0b-project", "ch-2")
        story_states_dao.update_story_state("p0b-project", {"source": "author-confirmed"})
        second = chapter_contract_service.get_or_compile_chapter_contract("p0b-project", "ch-2")

    assert first["story_state_version"] == 1
    assert second["story_state_version"] == 2
    assert second["version"] == first["version"] + 1


def test_generation_context_consumes_compiled_contract():
    from app.services import novel_generation_service as ngs

    with patch.object(
        ngs.chapter_contract_service,
        "get_or_compile_chapter_contract",
        return_value={
            "goal": "完成救援",
            "conflict": "守门人阻拦",
            "state_change": "林芜获得自由",
            "characters": ["沈砚", "林芜"],
            "key_events": ["破门"],
            "must_payoff": ["旧债"],
            "must_avoid": ["跳过冲突"],
            "ending_hook": "远处传来钟声",
            "pacing": "escalation",
            "version": 1,
        },
    ):
        prompt = ngs._build_context_before(
            project_id="p0b-project",
            chapter_id="ch-2",
            premise="追查师父",
            chapter={"id": "ch-2", "title": "救援", "summary": ""},
            chapter_index=1,
            total_chapters=3,
            outline_summary="主线",
            chapter_contract=ngs.chapter_contract_service.get_or_compile_chapter_contract(
                "p0b-project", "ch-2"
            ),
        )

    assert "【本章创作合同】" in prompt
    assert "完成救援" in prompt
    assert "守门人阻拦" in prompt
