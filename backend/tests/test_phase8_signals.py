import json
from pathlib import Path

from app.services import chapter_change_service, content_signals_service, entity_catalog_service
from app.services import story_state_transition_service as transitions


FIXTURE = Path(__file__).resolve().parents[1] / "experiment_fixtures" / "phase8" / "signal_fixture.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_phase8_fixture_propagates_canonical_state_relationship_and_knowledge_changes():
    fixture = _fixture()
    catalog = entity_catalog_service.build_character_catalog(fixture["entities"])
    contract = {
        "characters": ["character_lin_qiao", "character_mei_su"],
        "arc_context": {"entity_catalog": catalog},
        "key_events": [],
        "must_payoff": [],
    }
    observed = [transitions.extract_observed_state(row["content"], contract) for row in fixture["chapters"]]

    assert observed[0]["characters"] == ["character_lin_qiao", "character_mei_su"]
    assert observed[0]["scene_participants"] == ["character_lin_qiao", "character_mei_su"]
    assert observed[1]["relationships"] == ["character_lin_qiao|character_mei_su|conflict"]
    assert observed[2]["knowledge"] == ["character_lin_qiao|secret_river_debt"]
    assert observed[2]["state_changes"] == ["character_lin_qiao|knows_secret_river_debt"]

    character_signal = content_signals_service.character_consistency_signal(
        fixture["chapters"],
        [{"chapter_id": row["id"], **contract} for row in fixture["chapters"]],
    )
    assert character_signal == {
        "expected": 6,
        "mentioned": 5,
        "mention_coverage": 0.8333,
        "present": 5,
        "presence_coverage": 0.8333,
    }

    transition_rows = [
        {"chapter_id": chapter["id"], "status": "applied", "observed_state": state,
         "relationship_changes": state["relationships"], "knowledge_gained": state["knowledge"]}
        for chapter, state in zip(fixture["chapters"], observed)
    ]
    report = chapter_change_service.build_change_report(fixture["chapters"], transition_rows)
    signals = content_signals_service.build_long_form_signals(fixture["chapters"], transition_rows, [], [])

    assert report["metrics"]["chapter_change_rate"] == 1.0
    assert signals["relationship_change_rate"] == 0.6667
    assert signals["knowledge_change_rate"] == 0.3333


def test_phase8_foreshadow_signal_distinguishes_introduced_progressed_and_paid_off():
    fixture = _fixture()
    lifecycle = content_signals_service.foreshadow_lifecycle([fixture["foreshadow"]])

    assert lifecycle == {
        "introduced": 1,
        "progressed": 1,
        "paid_off": 1,
        "unverified": 0,
    }
