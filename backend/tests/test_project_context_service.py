from unittest.mock import patch

from app.services import project_context_service as context_service


def _project() -> dict:
    return {
        "id": "p1",
        "name": "雾城档案",
        "genre": "mystery",
        "premise": "档案修复师追查失踪案。",
        "characters": [
            {
                "id": "hero",
                "name": "沈砚",
                "role": "protagonist",
                "surfaceMotivation": "查清母亲失踪真相",
                "deepDesire": "确认自己的记忆可信",
                "speechStyle": "短句，先问证据",
            },
            {
                "id": "ally",
                "name": "林芜",
                "role": "supporting",
                "surfaceMotivation": "阻止档案被销毁",
                "speechStyle": "先说结论，再隐去来源",
            },
        ],
        "worldRules": [
            {"id": "rule-1", "name": "记忆修订", "description": "修订会留下铜锈气味。"},
        ],
        "styleConfig": {"name": "冷硬悬疑", "tone": "克制、具体"},
        "outline": {
            "mainSummary": "沈砚逐步发现母亲失踪与记忆修订计划有关。",
            "nodes": [
                {"id": "v1", "type": "volume", "title": "旧档案", "order": 1},
                {
                    "id": "c2",
                    "parentId": "v1",
                    "type": "chapter",
                    "title": "第二章 铜锈",
                    "summary": "沈砚检查铜钥匙。",
                    "conflict": "林芜拒绝透露钥匙来源。",
                    "revelation": "钥匙沾有修订后的铜锈。",
                    "emotion": "戒备",
                    "order": 2,
                },
            ],
        },
    }


def _chapters() -> list[dict]:
    return [
        {
            "id": "c1",
            "projectId": "p1",
            "title": "第一章 失窃",
            "order": 1,
            "summary": "原始档案失窃，沈砚发现一把铜钥匙。",
            "content": "沈砚把铜钥匙封进证物袋。" + "旧案细节" * 80,
        },
        {
            "id": "c2",
            "projectId": "p1",
            "title": "第二章 铜锈",
            "order": 2,
            "summary": "当前章节",
            "content": "数据库中的旧草稿。",
        },
        {
            "id": "c3",
            "projectId": "p1",
            "title": "第三章 真相",
            "order": 3,
            "summary": "未来章节揭示幕后人。",
            "content": "未来剧透：林芜是幕后人。",
        },
    ]


def _assemble(_capture: dict | None = None, **overrides):
    project_data = overrides.pop("_project_data", _project())
    entity_state = overrides.pop("_entity_state", "【实体状态】沈砚持有铜钥匙。")
    foreshadows = overrides.pop("_foreshadows", [
        {"id": "f1", "name": "钥匙来源", "description": "林芜隐瞒钥匙来源", "status": "progressing"},
        {
            "id": "f-future",
            "name": "未来伏笔",
            "description": "第三章才埋设的伏笔",
            "status": "planted",
            "plant_chapter_id": "c3",
        },
    ])
    state_changes = overrides.pop("_state_changes", [
        {"id": "state-1", "content": "沈砚与林芜的信任下降。"},
        {"id": "state-future", "content": "未来状态：林芜身份公开。", "chapter_id": "c3"},
    ])
    kwargs = {
        "project_id": "p1",
        "chapter_id": "c2",
        "current_text": "未保存正文：沈砚闻到铜锈气味。" + "现场动作" * 150,
        "selection_text": "林芜说：钥匙不是我的。",
        "query": "检查钥匙与林芜的设定是否矛盾",
        "profile": "consistency_check",
    }
    kwargs.update(overrides)
    with (
        patch.object(context_service.projects_dao, "get_project", return_value=project_data) as get_project,
        patch.object(context_service.chapters_dao, "list_chapters_global_order", return_value=_chapters()) as list_chapters,
        patch.object(context_service.memory_entities_dao, "build_entity_state_block", return_value=entity_state),
        patch.object(context_service.memory_fts_dao, "search_facts", return_value=[
            {"id": "fact-1", "content": "沈砚已经取得铜钥匙。", "chapter_id": "c1"},
            {
                "id": "fact-old",
                "content": "过期事实：铜钥匙仍在林芜手中。",
                "chapter_id": "c1",
                "status": "superseded",
            },
            {
                "id": "fact-future",
                "content": "未来事实：林芜在第三章公开幕后身份。",
                "chapter_id": "c3",
                "status": "active",
            },
        ]) as search_facts,
        patch.object(context_service.memory_fts_dao, "search_relevant", return_value=[
            {"chapter_id": "c3", "chapter_order": 3, "title": "第三章 真相", "snippet": "未来剧透"},
            {"chapter_id": "c1", "chapter_order": 1, "title": "第一章 失窃", "snippet": "铜钥匙被封存"},
        ]),
        patch.object(context_service.novel_facts_dao, "list_recent_state_changed_facts", return_value=state_changes),
        patch.object(context_service.foreshadows_dao, "list_active_foreshadows", return_value=foreshadows),
    ):
        result = context_service.assemble_project_context(**kwargs)
        if _capture is not None:
            _capture.update(
                get_project=get_project.call_args,
                list_chapters=list_chapters.call_args,
                search_facts=search_facts.call_args,
            )
        return result


def test_assemble_context_uses_four_bounded_layers_and_keeps_high_priority_data():
    result = _assemble()

    assert result["profile"] == "consistency_check"
    assert result["chars"] <= result["budget"]
    assert {block["layer"] for block in result["blocks"]} == {"L1", "L2", "L3", "L4"}
    assert "沈砚已经取得铜钥匙" in result["text"]
    assert "过期事实" not in result["text"]
    assert "未来事实" not in result["text"]
    assert "未来状态" not in result["text"]
    assert "未来伏笔" not in result["text"]
    assert "林芜说：钥匙不是我的" in result["text"]
    assert "未保存正文" in result["text"]


def test_assemble_context_excludes_current_and_future_chapters_from_history():
    result = _assemble()

    assert "铜钥匙被封存" in result["text"]
    assert "未来剧透" not in result["text"]
    assert "林芜是幕后人" not in result["text"]


def test_scoped_prose_context_excludes_global_plot_and_unscoped_memory():
    project = _project()
    project['premise'] = 'GLOBAL_PREMISE_SPOILER'
    project['outline']['mainSummary'] = 'GLOBAL_OUTLINE_SPOILER'
    project['outline']['nodes'].extend([
        {'id': 'a1', 'type': 'arc', 'parentId': 'v1', 'title': '当前弧'},
        {'id': 'c1', 'type': 'chapter', 'order': 1, 'arcId': 'a1'},
        {'id': 'c2', 'type': 'chapter', 'order': 2, 'arcId': 'a1'},
    ])
    project['characters'][0]['deepDesire'] = 'CHARACTER_SECRET_SPOILER'
    result = _assemble(_project_data=project, _entity_state='GLOBAL_ENTITY_SPOILER',
                       scoped_prose=True)
    assert 'SPOILER' not in result['text']
    assert '原始档案失窃' in result['text']
    assert '沈砚' in result['text']
    assert not ({'outline', 'story_state', 'entities', 'foreshadows', 'chapter_contract'}
                & {b['key'] for b in result['blocks']})


def test_scoped_prose_missing_history_position_does_not_include_all_chapters():
    result = _assemble(chapter_id='new-outline-chapter', scoped_prose=True)
    assert '未来剧透' not in result['text']
    assert '铜钥匙被封存' not in result['text']


def test_assemble_context_reads_only_requested_project():
    calls: dict = {}
    _assemble(_capture=calls)

    assert calls["get_project"].args == ("p1",)
    assert calls["list_chapters"].args == ("p1",)
    assert calls["search_facts"].args[0] == "p1"


def test_profile_inference_is_deterministic():
    assert context_service.infer_context_profile("帮我检查人物设定矛盾") == "consistency_check"
    assert context_service.infer_context_profile("给三个下一章走向") == "plot_planning"
    assert context_service.infer_context_profile("接着写当前场景") == "continuation"
    assert context_service.infer_context_profile("增强这段人物情绪") == "scene_writing"
    assert context_service.infer_context_profile("任意问题", "unknown") == "scene_writing"


def test_missing_project_fails_without_calling_llm():
    with patch.object(context_service.projects_dao, "get_project", return_value=None):
        try:
            context_service.assemble_project_context(project_id="missing", query="测试")
        except context_service.ProjectContextError as exc:
            assert "项目未找到" in str(exc)
        else:
            raise AssertionError("missing project must fail")


def test_assembler_can_skip_current_text_when_caller_already_supplies_it():
    result = _assemble(include_current_text=False)

    keys = {block["key"] for block in result["blocks"]}
    assert "current_text" not in keys
    assert "chapter" in keys
    assert "facts" in keys


def test_total_budget_does_not_trim_current_context_after_metadata_is_built():
    project = _project()
    project["premise"] = "核心设定" * 200
    project["characters"] = [
        {
            "name": f"人物{index}",
            "role": "supporting",
            "surfaceMotivation": "长期目标" * 80,
            "deepDesire": "深层欲望" * 80,
            "speechStyle": "独特声线" * 80,
        }
        for index in range(8)
    ]
    project["worldRules"] = [
        {"name": f"规则{index}", "description": "世界规则说明" * 100}
        for index in range(8)
    ]
    project["outline"]["mainSummary"] = "主线推进" * 500
    result = _assemble(
        _project_data=project,
        _entity_state="实体状态" * 300,
        _foreshadows=[
            {"id": "f1", "name": "长伏笔", "description": "伏笔说明" * 200, "status": "progressing"},
        ],
        _state_changes=[
            {"id": "s1", "content": "状态变化" * 200},
        ],
        profile="consistency_check",
        selection_text="选区：" + "关键句" * 210,
        current_text="正文" * 900 + "CURRENT_END_MARKER",
    )

    assert result["chars"] <= result["budget"]
    assert "current_text" in {block["key"] for block in result["blocks"]}
    assert "CURRENT_END_MARKER" in result["text"]
    block_chars = sum(block["chars"] for block in result["blocks"])
    separators = max(0, len(result["blocks"]) - 1) * 2
    assert result["chars"] >= block_chars + separators
