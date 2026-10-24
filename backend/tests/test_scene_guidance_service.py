from app.services import scene_guidance_service


def test_scene_guidance_renders_hard_truth_before_soft_craft_guidance():
    guidance = scene_guidance_service.build_scene_guidance(
        characters=[
            {
                "name": "林乔",
                "role": "protagonist",
                "gender": "female",
                "basicInfo": {"identity": "河务司信使"},
            }
        ],
        world_rules=[{"name": "盐灯", "description": "打开会夺走无关者的记忆"}],
        chapter_contract={
            "goal": "把盐灯交给梅苏",
            "conflict": "河务司封桥",
            "state_change": "梅苏得知盐灯的代价",
            "must_avoid": ["违背已确认事实"],
            "ending_hook": "桥下传来敲击声",
        },
        story_state={
            "current_phase_goal": "护送盐灯过桥",
            "main_conflict": "封桥导致交接受阻",
            "relationship_changes": ["林乔与梅苏互相信任"],
            "last_major_state_change": "盐灯已经被激活",
        },
    )

    assert guidance.startswith("【Hard Story Truth】")
    assert "林乔：身份=河务司信使；性别=female" in guidance
    assert "盐灯：打开会夺走无关者的记忆" in guidance
    assert "当前目标：把盐灯交给梅苏" in guidance
    assert "已确认关系：林乔与梅苏互相信任" in guidance
    assert "【Scene Plan / Soft Craft Guidance】" in guidance
    assert guidance.index("【Hard Story Truth】") < guidance.index("【Scene Plan / Soft Craft Guidance】")
    assert "Hard Story Truth 的优先级高于 Soft Craft Guidance" in guidance
    assert "不要新增当前上下文未提供、且会改变世界规则、人物背景或剧情事实的重要设定" in guidance


def test_scene_guidance_omits_empty_hard_truth_values_without_inventing_facts():
    guidance = scene_guidance_service.build_scene_guidance(
        characters=[{"name": "无名人"}],
        world_rules=[],
        chapter_contract={},
        story_state={},
    )

    assert "无名人" in guidance
    assert "性别=" not in guidance
    assert "未提供的信息不得补写" in guidance
