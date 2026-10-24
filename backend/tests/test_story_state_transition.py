from unittest.mock import patch

from app.services import story_state_transition_service as transitions


def test_extract_observed_state_uses_explicit_evidence_only():
    observed = transitions.extract_observed_state(
        "沈砚走入密室。\n关系变化：沈砚与林芜暂时互信\n状态变化：取得铜钥匙\n下一目标：查找暗门",
        {
            "characters": ["沈砚", "林芜"],
            "key_events": ["取得铜钥匙", "打开天门"],
            "must_payoff": ["钥匙来源"],
        },
    )
    assert observed["characters"] == ["沈砚", "林芜"]
    assert observed["knowledge"] == ["取得铜钥匙"]
    assert observed["foreshadows"] == []
    assert observed["relationships"] == ["沈砚与林芜暂时互信"]
    assert observed["state_changes"] == ["取得铜钥匙"]
    assert observed["next_goals"] == ["查找暗门"]


def test_conflicting_observation_is_reported_without_state_mutation():
    state = {"version": 2, "relationship_changes": [], "current_mysteries": [], "unresolved_questions": [], "next_stage_directions": []}
    project = {"id": "p1", "outline": {"nodes": [{"id": "c1", "type": "chapter"}]}}
    chapter = {"id": "c1", "content": "状态冲突：主角身份无法确认。", "summary": "摘要"}
    contract = {"state_change": "主角身份确认", "characters": [], "key_events": [], "must_payoff": [], "conflict": "身份冲突"}
    with (
        patch.object(transitions.story_state_transitions_dao, "get_transition", return_value=None),
        patch.object(transitions.projects_dao, "get_project", return_value=project),
        patch.object(transitions.chapters_dao, "get_chapter", return_value=chapter),
        patch.object(transitions.story_state_service, "ensure_story_state", return_value=state),
        patch.object(transitions.chapter_contract_service, "get_or_compile_chapter_contract", return_value=contract),
        patch.object(transitions.foreshadows_dao, "list_active_foreshadows", return_value=[]),
        patch.object(transitions.chapters_dao, "list_chapters_global_order", return_value=[chapter]),
        patch.object(transitions.story_state_service, "update_story_state") as update,
        patch.object(transitions.story_state_transitions_dao, "save_transition", return_value={"status": "conflict"}) as save,
    ):
        result = transitions.apply_chapter_transition("p1", "c1", content=chapter["content"])

    assert result["status"] == "conflict"
    update.assert_not_called()
    fields = save.call_args.args[2]
    assert fields["final_state"]["version"] == 2
    assert fields["conflicts"]


def test_resolve_conflict_applies_candidate_only_after_approval():
    transition = {
        "status": "conflict",
        "final_state": {"version": 2, "relationship_changes": ["互信"], "current_mysteries": []},
    }
    updated = {"version": 3, "relationship_changes": ["互信"], "current_mysteries": []}
    with (
        patch.object(transitions.story_state_transitions_dao, "get_transition", return_value=transition),
        patch.object(transitions.story_state_service, "update_story_state", return_value=updated) as update,
        patch.object(transitions.story_state_transitions_dao, "update_decision", return_value={"status": "applied"}) as decide,
    ):
        result = transitions.resolve_state_conflict("p1", "c1", approve=True)

    assert result["status"] == "applied"
    update.assert_called_once()
    decide.assert_called_once()


def test_conflict_resolution_reject_keeps_story_state_unchanged():
    transition = {
        "status": "conflict",
        "final_state": {"version": 2, "relationship_changes": ["候选变化"]},
    }
    with (
        patch.object(transitions.story_state_transitions_dao, "get_transition", return_value=transition),
        patch.object(transitions.story_state_service, "update_story_state") as update,
        patch.object(transitions.story_state_transitions_dao, "update_decision", return_value={"status": "rejected"}) as decide,
    ):
        result = transitions.resolve_state_conflict("p1", "c1", approve=False)

    assert result["status"] == "rejected"
    update.assert_not_called()
    decide.assert_called_once_with(
        "p1", "c1", status="rejected", final_state=transition["final_state"]
    )


def test_apply_chapter_transition_updates_next_stage_and_records_audit():
    state = {
        "version": 3,
        "unresolved_questions": ["旧问题"],
        "relationship_changes": ["互不信任"],
        "next_stage_directions": [],
    }
    updated = {**state, "version": 4}
    project = {
        "id": "p1",
        "outline": {"nodes": [{"id": "c2", "summary": "进入第二阶段。"}]},
    }
    chapter = {"id": "c1", "summary": "本章摘要"}
    contract = {
        "state_change": "主角拿到关键证据",
        "ending_hook": "门后有人呼救",
        "key_events": ["拿到证据"],
        "conflict": "守门人追来",
    }
    with (
        patch.object(transitions.story_state_transitions_dao, "get_transition", return_value=None),
        patch.object(transitions.projects_dao, "get_project", return_value=project),
        patch.object(transitions.chapters_dao, "get_chapter", return_value=chapter),
        patch.object(transitions.story_state_service, "ensure_story_state", return_value=state),
        patch.object(transitions.chapter_contract_service, "get_or_compile_chapter_contract", return_value=contract),
        patch.object(transitions.foreshadows_dao, "list_active_foreshadows", return_value=[{"name": "黑匣子"}]),
        patch.object(transitions.chapters_dao, "list_chapters_global_order", return_value=[chapter, {"id": "c2"}]),
        patch.object(transitions.story_state_service, "update_story_state", return_value=updated) as update,
        patch.object(transitions.story_state_transitions_dao, "save_transition", return_value={"id": "t1"}) as save,
    ):
        result = transitions.apply_chapter_transition("p1", "c1", summary="摘要")

    assert result["id"] == "t1"
    fields = update.call_args.args[1]
    assert fields["last_major_state_change"] == "主角拿到关键证据"
    assert fields["active_foreshadows"] == ["黑匣子"]
    assert "门后有人呼救" in fields["unresolved_questions"]
    assert fields["next_stage_directions"] == ["进入第二阶段。"]
    assert save.call_args.args[:2] == ("p1", "c1")


def test_apply_chapter_transition_is_idempotent():
    existing = {"id": "t1", "chapter_id": "c1"}
    with patch.object(transitions.story_state_transitions_dao, "get_transition", return_value=existing):
        assert transitions.apply_chapter_transition("p1", "c1") is existing
