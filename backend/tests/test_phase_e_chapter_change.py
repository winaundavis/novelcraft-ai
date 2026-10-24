from app.services import chapter_change_service as changes


def test_measure_change_counts_only_new_observed_evidence():
    previous = {"observed_state": {"state_changes": ["opened gate"], "relationships": ["trust"]}}
    current = {"observed_state": {"state_changes": ["opened gate", "lost key"], "relationships": ["trust", "alliance"]}}
    result = changes.measure_change(current, previous)
    assert result["metrics"]["state_change_count"] == 1
    assert result["metrics"]["relationship_change_count"] == 1
    assert result["total_change_count"] == 2


def test_repetition_reports_evidence_without_changing_modes():
    chapters = [
        {"id": "c1", "content": "The door opened slowly. The guard watched."},
        {"id": "c2", "content": "The door opened slowly. The guard watched."},
        {"id": "c3", "content": "The door opened slowly. The guard watched."},
    ]
    contracts = [{"chapter_id": "c1", "chapter_mode": "Reveal"}, {"chapter_id": "c2", "chapter_mode": "Reveal"}, {"chapter_id": "c3", "chapter_mode": "Reveal"}]
    result = changes.detect_repetition(chapters, contracts)
    assert result["mode_run"] == 3
    assert "repeated_chapter_mode" in result["reasons"]
    assert result["repeated_sentences"]


def test_change_report_recommends_pause_after_repeated_no_change():
    chapters = [{"id": f"c{i}", "content": f"Chapter {i}."} for i in range(1, 4)]
    transitions = [{"chapter_id": f"c{i}", "observed_state": {}} for i in range(1, 4)]
    result = changes.build_change_report(chapters, transitions)
    assert result["stop_recommended"] is True
    assert "consecutive_chapters_without_observed_change" in result["stop_reasons"]
    assert result["api_calls"] == 0


def test_change_report_does_not_flag_single_repetition_as_stop():
    chapters = [{"id": "c1", "content": "A different scene."}, {"id": "c2", "content": "A repeated scene."}]
    result = changes.build_change_report(chapters, [])
    assert result["stop_recommended"] is False
