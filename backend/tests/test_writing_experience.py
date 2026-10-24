from unittest.mock import patch

from app.services import writing_experience_service


def test_single_observation_stays_candidate_and_three_verified_observations_can_be_accepted():
    rows = []

    def record(project_id, experience_type, observation, preferred_fix, source_chapters):
        kwargs = {"project_id": project_id, "experience_type": experience_type, "observation": observation, "preferred_fix": preferred_fix, "source_chapters": source_chapters}
        existing = next((item for item in rows if item["experience_type"] == kwargs["experience_type"] and item["observation"] == kwargs["observation"]), None)
        if existing:
            existing["evidence_count"] += 1
            return existing
        item = {"id": "x1", "status": "candidate", "evidence_count": 1, **kwargs}
        rows.append(item)
        return item

    with patch.object(writing_experience_service.experience_dao, "record_observation", side_effect=record), patch.object(writing_experience_service.experience_dao, "accept", side_effect=lambda *_args: rows[0].update(status="accepted") or rows[0]):
        first = writing_experience_service.record("p1", "recurring_problem", "战斗规则解释过载。", "用动作和结果展示规则。", ["c1"])
        writing_experience_service.record("p1", "recurring_problem", "战斗规则解释过载。", "用动作和结果展示规则。", ["c2"])
        third = writing_experience_service.record("p1", "recurring_problem", "战斗规则解释过载。", "用动作和结果展示规则。", ["c3"])
        assert first["status"] == "candidate"
        assert third["evidence_count"] == 3
        assert writing_experience_service.accept("p1", "x1")["status"] == "accepted"


def test_accepted_prompt_contains_only_accepted_experiences_and_is_bounded():
    with patch.object(writing_experience_service.experience_dao, "list_accepted", return_value=[
        {"experience_type": "successful_pattern", "observation": "先行动后解释。", "preferred_fix": "让结果承载规则。"},
        {"experience_type": "recurring_problem", "observation": "不要写入密钥。", "preferred_fix": "删除敏感信息。", "status": "candidate"},
    ]):
        text = writing_experience_service.render_accepted("p1")
    assert "先行动后解释" in text
    assert "不要写入密钥" not in text
    assert len(text) <= writing_experience_service.EXPERIENCE_RENDER_BUDGET
