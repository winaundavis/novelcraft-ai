from app.services.style_engine import profile_service


GENRES = ("urban", "xuanhuan", "mystery", "romance", "history", "scifi", "unlimited_flow", "light_novel")
REQUIRED = ("readerExpectations", "conflictTypes", "pacingTendency", "informationRelease", "chapterChange", "smallArc", "bigArc", "commonRisks")


def test_all_genre_profiles_are_reusable_and_complete():
    for genre in GENRES:
        profile = profile_service.build_genre_style_profile(genre)
        assert profile["genre"] == genre
        assert profile["styleKind"] == "genre"
        assert all(profile["genreRules"].get(key) for key in REQUIRED)
        assert "author" not in str(profile).lower()


def test_aliases_are_deterministic_and_do_not_create_new_engine_paths():
    assert profile_service.build_genre_style_profile("fantasy")["genre"] == "xuanhuan"
    assert profile_service.build_genre_style_profile("historical")["genre"] == "history"
    assert profile_service.build_genre_style_profile("mystery") == profile_service.build_genre_style_profile("mystery")


def test_style_switch_preserves_story_state_and_changes_expression_contract():
    state = {
        "version": 4,
        "main_conflict": "find the missing witness",
        "relationship_changes": ["trust"],
        "current_mysteries": ["who sent the letter"],
        "active_foreshadows": ["red seal"],
    }
    source = profile_service.build_genre_style_profile("mystery")
    target = profile_service.build_genre_style_profile("romance")
    result = profile_service.validate_style_switch(state, dict(state), source, target)
    assert result["valid"] is True
    assert result["story_preserved"] is True
    assert result["style_changed"] is True
    assert result["api_calls"] == 0


def test_style_switch_rejects_story_state_mutation():
    state = {"main_conflict": "A", "current_mysteries": ["Q"]}
    changed = {"main_conflict": "B", "current_mysteries": ["Q"]}
    result = profile_service.validate_style_switch(
        state, changed,
        profile_service.build_genre_style_profile("mystery"),
        profile_service.build_genre_style_profile("romance"),
    )
    assert result["valid"] is False
    assert "story_state_changed" in result["issues"]


def test_style_prompt_is_a_preference_not_a_story_truth_override():
    contract = profile_service.compile_contract(
        {"name": "Concise", "tone": "clean"}, None, None,
    )
    prompt = profile_service.contract_to_prompt_block(contract)
    assert "【风格偏好】" in prompt
    assert "不得覆盖故事事实、人物身份、世界规则、时间线或已确认关系" in prompt
    assert "硬约束" not in prompt
