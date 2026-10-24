from unittest.mock import patch

from app.services import content_signals_service, readiness_service


def test_emotion_and_pacing_are_descriptive_zero_api_signals():
    contracts = [{"chapter_id": "c1", "emotion": "tense", "conflict": "x", "ending_hook": "y"}, {"chapter_id": "c2", "emotion": "tense", "chapter_mode": "daily"}]
    assert content_signals_service.emotion_timeline(contracts)[1]["consecutive_same_emotion"] == 2
    assert content_signals_service.pacing_timeline(contracts)[0]["hook"] is True


def test_readiness_leaves_cost_null_without_audit_data():
    with patch.object(readiness_service.chapters_dao, "list_chapters_global_order", return_value=[]), patch.object(readiness_service.chapter_contracts_dao, "list_latest_contracts", return_value=[]), patch.object(readiness_service.story_state_transitions_dao, "list_transitions", return_value=[]), patch.object(readiness_service.foreshadows_dao, "list_foreshadows", return_value=[]), patch.object(readiness_service, "query_llm_audit", return_value=[]):
        result = readiness_service.build_readiness({"genre": "xuanhuan"}, "p")
    assert result["cost"]["metrics"]["calls_per_chapter"] is None
    assert result["style"]["metrics"]["label"] == "Style Compliance Signals"


def test_long_form_signals_are_descriptive_not_quality_scores():
    values = content_signals_service.build_long_form_signals(
        [{"id": "c1"}, {"id": "c2"}],
        [{"status": "applied", "knowledge_gained": ["fact"]}],
        [{"chapter_mode": "investigation", "emotion": "tense", "characters": ["Lin"]}, {"chapter_mode": "investigation", "emotion": "tense", "characters": ["Ning"]}],
        [{"status": "progressing"}],
    )
    assert values["chapter_mode_repeat_rate"] == 1.0
    assert values["emotion_repeat_run"] == 2
