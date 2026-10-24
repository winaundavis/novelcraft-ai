from app.services.content_benchmark_service import baseline_fixture, broken_fixture, compare_baseline, detect_regressions, measure_fixture


def test_semantic_baseline_and_broken_fixture_expose_real_regressions():
    baseline, broken = baseline_fixture(), broken_fixture()
    delta = compare_baseline(measure_fixture(baseline), measure_fixture(broken))
    issues = detect_regressions(baseline, broken)
    assert delta["chapter_state_coverage"]["delta"] < 0
    assert {"missing_state:c2", "relationship_conflict:c2", "repeated_chapter_text:c2", "missing_payoff:c2", "voice_changed:c3", "style_changed:c3"} <= set(issues)
