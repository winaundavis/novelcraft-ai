from app.services import quality_gap_service


def test_quality_gap_fixture_changes_only_candidate_quality_layer():
    prepared = quality_gap_service.prepare_quality_gap_fixture()
    baseline = prepared["requests"]["baseline"]
    candidate = prepared["requests"]["candidate"]

    assert prepared["real_llm_calls"] == 0
    assert prepared["call_plan"] == {"primary_calls": 2, "retries": 0, "rewrites": 0, "qa": 0}
    assert baseline["scene"] == candidate["scene"]
    assert "场景具身合同" not in baseline["user_prompt"]
    assert "场景具身合同" in candidate["user_prompt"]
    assert "优先通过动作、环境、感官与选择表现情绪" in candidate["user_prompt"]
    assert "章末合同" in candidate["user_prompt"]


def test_quality_signals_warn_about_exposition_without_rating_literary_quality():
    text = """她解释自己为什么不肯交灯，因为这是旧城唯一的规则。\n\n他又解释河务司为何封桥，原因是债务要被清算。\n\n叙述者再次解释这意味着谁都不能离开。"""

    signals = quality_gap_service.evaluate_quality_signals(text, ["new_danger"])

    assert signals["exposition_risk"] is True
    assert signals["exposition_paragraph_run"] == 3
    assert signals["ending_contract_evidence"] == []
    assert signals["is_literary_judgment"] is False
