from app.services import phase8b_quality_service


def test_phase8b_fixture_keeps_scene_and_canonical_facts_identical_across_variants():
    prepared = phase8b_quality_service.prepare_phase8b_fixture()
    requests = prepared["requests"]

    assert set(requests) == {"A", "B", "C"}
    assert len({row["scene_hash"] for row in requests.values()}) == 1
    assert len({row["fact_hash"] for row in requests.values()}) == 1
    assert requests["A"]["scene"] == requests["B"]["scene"] == requests["C"]["scene"]
    assert "【场景具身合同】" not in requests["A"]["user_prompt"]
    assert "【场景具身合同】" in requests["B"]["user_prompt"]
    assert "【Canonical Facts】" in requests["C"]["user_prompt"]


def test_phase8b_soft_layer_includes_facts_without_overriding_hard_contract():
    candidate = phase8b_quality_service.prepare_phase8b_fixture()["requests"]["C"]
    prompt = candidate["user_prompt"]

    assert "林乔：female" in prompt
    assert "梅苏：female" in prompt
    assert "盐灯打开会夺走一个无关者的记忆" in prompt
    assert "【Hard】" in prompt
    assert "【Soft】" in prompt
    assert "【Exit】" in prompt
    assert "Soft 不得覆盖 Hard" in prompt
    assert "优先考虑使用动作、环境、感官与人物选择增强体验" in prompt
    assert "不要新增当前上下文未提供、且会改变世界规则、人物背景或剧情事实的重要设定" in prompt
