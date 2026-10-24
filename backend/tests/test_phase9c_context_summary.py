"""Phase 9-C: user-facing context summary must use assembled context only."""

from app.services import novel_generation_service as ngs


def test_context_reference_summary_counts_assembled_inputs_only():
    summary = ngs._context_reference_summary(
        characters=[{"name": "林乔"}, {"name": "梅苏"}],
        memory_context={
            "recent_chapter_summaries": ["第一章"],
            "entity_state": "林乔：信使",
            "active_foreshadows": "【未回收伏笔】\n- 盐灯\n- 封桥",
            "timeline": "第二章之前",
        },
        style_contract={"preset": {"name": "现实 / 克制"}},
        project_id="project-1",
    )

    assert summary == "本次生成已参考：人物 2、故事状态 3、伏笔 2、当前文风 1"
