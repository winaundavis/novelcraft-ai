from unittest.mock import patch

from app.services import commercial_pacing_service, readiness_service


def _project():
    return {
        "id": "pacing-project",
        "outline": {
            "mainSummary": "查明旧案并阻止灾难。",
            "nodes": [
                {"id": "c1", "summary": "进入城中", "mode": "Action", "hook": "门后传来钟声。"},
                {"id": "c2", "summary": "追查线索", "hook": "线索指向同伴。"},
                {"id": "c3", "summary": "面对敌人", "mode": "Conflict", "hook": "敌人说出了真名。"},
            ],
            "smallArcs": [{"start": 0, "end": 2, "goal": "找到旧案入口", "payoff": "获得密钥"}],
            "bigArc": {"goal": "阻止灾难", "climax": "城门决战", "nextGoal": "寻找失踪者"},
        },
        "characters": [],
    }


def test_compile_pacing_has_three_scales_and_avoids_consecutive_modes():
    project = _project()
    first = commercial_pacing_service.compile_pacing_context(project, "c1", {"current_volume_goal": "阻止灾难"})
    second = commercial_pacing_service.compile_pacing_context(project, "c2", {"current_volume_goal": "阻止灾难"})

    assert first["chapter_mode"] == "Action"
    assert second["chapter_mode"] != first["chapter_mode"]
    assert second["chapter_scale"]["goal"] == "追查线索"
    assert second["small_arc"]["goal"] == "找到旧案入口"
    assert second["big_arc"]["goal"] == "阻止灾难"


def test_explicit_mode_is_normalized_and_recent_modes_are_deterministic():
    project = _project()
    project["outline"]["nodes"][1]["chapterMode"] = "reveal"
    result = commercial_pacing_service.compile_pacing_context(project, "c2", {})
    assert result["chapter_mode"] == "Reveal"
    assert result["recent_modes"] == ["Action"]


def test_contract_rhythm_is_bounded():
    rendered = commercial_pacing_service.render_pacing_context(
        {"chapter_mode": "Investigation", "chapter_scale": {"goal": "查线索"}, "small_arc": {}, "big_arc": {}}
    )
    assert "章节模式：Investigation" in rendered
    assert len(rendered) <= commercial_pacing_service.PACING_RENDER_BUDGET


def test_readiness_uses_existing_data_without_llm():
    project = _project()
    chapters = [
        {"id": "c1", "status": "completed", "summary": "推进并留下钩子", "content": "正文"},
        {"id": "c2", "status": "completed", "summary": "获得变化", "content": "正文"},
        {"id": "c3", "status": "writing", "summary": "", "content": ""},
    ]
    with (
        patch.object(readiness_service.chapters_dao, "list_chapters_global_order", return_value=chapters),
        patch.object(readiness_service.chapter_contracts_dao, "list_latest_contracts", return_value=[
            {"chapter_id": "c1", "chapter_mode": "Action", "arc_context": {}},
            {"chapter_id": "c2", "chapter_mode": "Reveal", "arc_context": {}},
        ]),
        patch.object(readiness_service.story_state_transitions_dao, "list_transitions", return_value=[{"chapter_id": "c1"}]),
        patch.object(readiness_service.foreshadows_dao, "list_foreshadows", return_value=[]),
    ):
        result = readiness_service.build_readiness(project, "pacing-project")

    assert result["api_calls"] == 0
    assert result["narrative"]["metrics"]["state_transition_rate"] == 0.5
    assert result["commercial"]["metrics"]["completed_chapters"] == 2
