from unittest.mock import patch

from app.services import character_voice_service


def _project():
    return {"id": "voice-project", "characters": [
        {"id": "a", "name": "阿宁", "role": "protagonist", "deepDesire": "保护妹妹", "speechStyle": "短句，先报结论", "habits": "紧张时数刀"},
        {"id": "b", "name": "陆沉", "role": "supporting", "deepDesire": "隐藏旧案", "speechStyle": "长句，常用反问", "habits": "说话前敲桌"},
    ]}


def test_seeded_voice_profiles_are_structured_and_distinct():
    with patch.object(character_voice_service.voice_dao, "list_profiles", return_value=[]), patch.object(character_voice_service.voice_dao, "create_profile") as create:
        profiles = character_voice_service.get_or_seed_profiles(_project())
    assert len(profiles) == 2
    assert profiles[0]["sentence_pattern"] != profiles[1]["sentence_pattern"]
    assert create.call_count == 2
    assert set(profiles[0]) >= {"character_id", "worldview", "speech_length", "information_strategy", "behavior_signature"}


def test_voice_prompt_is_bounded_and_only_includes_requested_characters():
    project = _project()
    with patch.object(character_voice_service.voice_dao, "list_profiles", return_value=[
        {"character_id": "a", "name": "阿宁", "worldview": "保护妹妹", "speech_length": "短", "sentence_pattern": "先报结论", "favorite_expressions": ["知道了"], "avoided_expressions": [], "emotional_expression": "压住情绪", "information_strategy": "先结论后细节", "behavior_signature": "紧张时数刀"},
        {"character_id": "b", "name": "陆沉", "worldview": "隐藏旧案", "speech_length": "长", "sentence_pattern": "常用反问", "favorite_expressions": [], "avoided_expressions": [], "emotional_expression": "绕开直说", "information_strategy": "只透露半步", "behavior_signature": "说话前敲桌"},
    ]):
        text = character_voice_service.render_voice_profiles(project, ["阿宁"])
    assert "阿宁" in text and "陆沉" not in text
    assert "先结论后细节" in text
    assert len(text) <= character_voice_service.VOICE_RENDER_BUDGET
