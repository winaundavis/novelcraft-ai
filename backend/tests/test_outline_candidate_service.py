from __future__ import annotations

import json

import pytest


def _outline() -> dict:
    return {
        "mainSummary": "主线",
        "nodes": [
            {"id": "v-1", "type": "volume", "parentId": None, "title": "第一卷", "order": 1},
            {"id": "a-1", "type": "arc", "parentId": "v-1", "title": "失去", "order": 1},
            {"id": "ch-1", "type": "chapter", "parentId": "v-1", "arcId": "a-1", "title": "第一章", "order": 1},
        ],
    }


def _six_chapter_outline() -> dict:
    outline = _outline()
    outline["nodes"] = outline["nodes"][:2] + [
        {
            "id": f"ch-{order}", "type": "chapter", "parentId": "v-1",
            "arcId": "a-1", "title": f"第{order}章", "order": order,
        }
        for order in range(1, 7)
    ]
    return outline


def test_patch_rejects_deleting_chapter_with_saved_content():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    project = {
        "id": "project-a",
        "outline": _outline(),
        "chapters": [{"id": "ch-1", "project_id": "project-a", "content": "已保存正文"}],
    }
    candidate = {"mainSummary": "主线", "nodes": []}

    with pytest.raises(OutlinePatchError, match="已有正文"):
        validate_outline_patch(project, candidate)


def test_detached_legacy_chapter_row_is_not_reported_as_candidate_removal():
    from app.services.outline_candidate_service import (
        protected_saved_chapter_removals,
        validate_outline_patch,
    )

    current = _outline()
    project = {
        "id": "project-a",
        "outline": current,
        "chapters": [{
            "id": "chapter-1",
            "project_id": "project-a",
            "title": "第一章",
            "chapter_order": 1,
            "content": "已保存正文",
        }],
    }

    assert protected_saved_chapter_removals(project, current) == []
    assert validate_outline_patch(project, current)["nodes"]


def test_patch_rejects_moving_chapter_with_saved_content_to_another_volume():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    current = _outline()
    current["nodes"].append({
        "id": "v-2", "type": "volume", "parentId": None,
        "title": "第二卷", "order": 2,
    })
    candidate = json.loads(json.dumps(current, ensure_ascii=False))
    candidate["nodes"][2]["parentId"] = "v-2"
    candidate["nodes"][2]["arcId"] = None
    project = {
        "id": "project-a",
        "outline": current,
        "chapters": [{"id": "ch-1", "project_id": "project-a", "content": "已保存正文"}],
    }

    with pytest.raises(OutlinePatchError) as exc:
        validate_outline_patch(project, candidate)

    assert exc.value.code == "saved_chapter_move_forbidden"


def test_patch_rejects_unknown_node_ids_instead_of_treating_them_as_new_nodes():
    from app.services.outline_candidate_service import OutlinePatchError, merge_outline_patch

    with pytest.raises(OutlinePatchError) as exc:
        merge_outline_patch(_outline(), {
            "patchMode": "merge",
            "upsertNodes": [{
                "id": "foreign-arc", "type": "arc", "parentId": "v-1",
                "title": "不属于当前大纲", "summary": "不应新增",
            }],
            "removeNodeIds": [],
        })

    assert exc.value.code == "node_reference_invalid"


def test_arc_replacements_create_server_owned_arcs_and_reassign_chapters():
    from app.services.outline_candidate_service import (
        normalize_model_outline_payload,
        validate_outline_patch,
    )

    current = _six_chapter_outline()
    candidate = normalize_model_outline_payload({
        "arcReplacements": [{
            "volumeId": "v-1",
            "arcs": [
                {"title": "怪异早餐", "summary": "一顿反常早餐开始并结束。", "startChapter": 1, "endChapter": 2},
                {"title": "错位课堂", "summary": "课堂规则错位，主角仍未在意。", "startChapter": 3, "endChapter": 4},
                {"title": "缓慢觉醒", "summary": "异常累积后主角终于开始怀疑现实。", "startChapter": 5, "endChapter": 6},
            ],
        }],
    }, current_outline=current)
    normalized = validate_outline_patch({"outline": current, "chapters": []}, candidate)

    arcs = sorted((node for node in normalized["nodes"] if node["type"] == "arc"), key=lambda node: node["order"])
    chapters = sorted((node for node in normalized["nodes"] if node["type"] == "chapter"), key=lambda node: node["order"])
    assert [arc["title"] for arc in arcs] == ["怪异早餐", "错位课堂", "缓慢觉醒"]
    assert all(arc["id"].startswith("generated-arc-") for arc in arcs)
    assert [chapter["arcId"] for chapter in chapters] == [
        arcs[0]["id"], arcs[0]["id"], arcs[1]["id"],
        arcs[1]["id"], arcs[2]["id"], arcs[2]["id"],
    ]
    assert "a-1" not in {node["id"] for node in normalized["nodes"]}


@pytest.mark.parametrize("arcs", [
    [
        {"title": "一", "summary": "一", "startChapter": 1, "endChapter": 3},
        {"title": "二", "summary": "二", "startChapter": 3, "endChapter": 6},
    ],
    [
        {"title": "一", "summary": "一", "startChapter": 1, "endChapter": 2},
        {"title": "二", "summary": "二", "startChapter": 4, "endChapter": 6},
    ],
])
def test_arc_replacements_reject_overlap_or_uncovered_chapters(arcs):
    from app.services.outline_candidate_service import OutlinePatchError, normalize_model_outline_payload

    with pytest.raises(OutlinePatchError) as exc:
        normalize_model_outline_payload({
            "arcReplacements": [{"volumeId": "v-1", "arcs": arcs}],
        }, current_outline=_six_chapter_outline())

    assert exc.value.code == "arc_range_invalid"


def test_validation_still_rejects_model_created_volumes_and_chapters():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    for node in (
        {"id": "generated-volume-x", "type": "volume", "title": "伪造卷", "order": 2},
        {"id": "generated-chapter-x", "type": "chapter", "parentId": "v-1", "title": "伪造章", "order": 7},
    ):
        candidate = _six_chapter_outline()
        candidate["nodes"].append(node)
        with pytest.raises(OutlinePatchError) as exc:
            validate_outline_patch({"outline": _six_chapter_outline(), "chapters": []}, candidate)
        assert exc.value.code == "node_reference_invalid"


def test_patch_reports_invalid_numeric_fields_with_a_safe_code():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    candidate = _outline()
    candidate["nodes"][0]["order"] = "not-a-number"

    with pytest.raises(OutlinePatchError) as exc:
        validate_outline_patch({"id": "project-a", "outline": _outline(), "chapters": []}, candidate)

    assert exc.value.code == "outline_field_invalid"


def test_patch_reports_parent_and_saved_delete_rejections_with_stable_codes():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    invalid_parent = _outline()
    invalid_parent["nodes"][1]["parentId"] = "missing"
    with pytest.raises(OutlinePatchError) as parent_error:
        validate_outline_patch({"id": "project-a", "outline": _outline(), "chapters": []}, invalid_parent)
    assert parent_error.value.code == "outline_parent_invalid"

    project = {
        "id": "project-a",
        "outline": _outline(),
        "chapters": [{"id": "ch-1", "project_id": "project-a", "content": "saved"}],
    }
    deleted = _outline()
    deleted["nodes"] = deleted["nodes"][:2]
    with pytest.raises(OutlinePatchError) as delete_error:
        validate_outline_patch(project, deleted)
    assert delete_error.value.code == "saved_chapter_delete_forbidden"


def test_patch_accepts_legacy_chapter_contract_fields_already_present_in_current_outline():
    from app.services.outline_candidate_service import validate_outline_patch

    current = _outline()
    current["nodes"][2].update({
        "goal": "发现异常",
        "events": ["医院见闻"],
        "hook": "血雨开始",
        "pacing": "加速",
        "stateChange": "从怀疑转为确认",
        "characters": ["主角"],
    })
    project = {"id": "project-a", "outline": current, "chapters": []}

    normalized = validate_outline_patch(project, current)

    assert normalized["nodes"][2]["goal"] == "发现异常"
    assert normalized["nodes"][2]["stateChange"] == "从怀疑转为确认"


def test_patch_rejects_arc_or_chapter_with_invalid_parent():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    candidate = _outline()
    candidate["nodes"][1]["parentId"] = "missing-volume"

    with pytest.raises(OutlinePatchError, match="父级"):
        validate_outline_patch({"id": "project-a", "outline": _outline(), "chapters": []}, candidate)


def test_patch_rejects_duplicate_node_ids():
    from app.services.outline_candidate_service import OutlinePatchError, validate_outline_patch

    candidate = _outline()
    candidate["nodes"][2]["id"] = "a-1"

    with pytest.raises(OutlinePatchError, match="重复"):
        validate_outline_patch({"id": "project-a", "outline": _outline(), "chapters": []}, candidate)


def test_outline_hash_is_stable_for_key_order():
    from app.services.outline_candidate_service import outline_hash

    left = {"mainSummary": "主线", "nodes": [{"id": "v1", "type": "volume", "title": "卷一"}]}
    right = {"nodes": [{"title": "卷一", "type": "volume", "id": "v1"}], "mainSummary": "主线"}

    assert outline_hash(left) == outline_hash(right)


def test_outline_diff_reports_added_removed_changed_and_moved_without_content():
    from app.services.outline_candidate_service import build_outline_diff

    previous = {
        "mainSummary": "旧主线",
        "nodes": [
            {"id": "v1", "type": "volume", "title": "卷一", "order": 1},
            {"id": "c1", "type": "chapter", "parentId": "v1", "title": "旧标题", "order": 1},
            {"id": "c2", "type": "chapter", "parentId": "v1", "title": "删除", "order": 2},
        ],
    }
    candidate = {
        "mainSummary": "新主线",
        "nodes": [
            {"id": "v1", "type": "volume", "title": "卷一", "order": 1},
            {"id": "c1", "type": "chapter", "parentId": "v1", "title": "新标题", "order": 3},
            {"id": "c3", "type": "chapter", "parentId": "v1", "title": "新增", "order": 2},
        ],
    }

    diff = build_outline_diff(previous, candidate)

    assert [item["id"] for item in diff["added"]] == ["c3"]
    assert [item["id"] for item in diff["removed"]] == ["c2"]
    assert diff["changed"][0]["id"] == "c1"
    assert diff["moved"][0]["id"] == "c1"
    assert diff["mainSummaryChanged"] is True
    assert "content" not in repr(diff).lower()


def test_model_patch_normalizer_filters_metadata_and_accepts_node_id_alias():
    from app.services.outline_candidate_service import normalize_model_outline_payload

    normalized = normalize_model_outline_payload({
        "patchMode": "merge",
        "explanation": "为什么这样修改",
        "upsertNodes": [
            {"nodeId": "a-1", "summary": "新的剧情弧", "reason": "增强因果"},
        ],
        "removeNodeIds": [],
    })

    assert normalized == {
        "patchMode": "merge",
        "upsertNodes": [{"id": "a-1", "summary": "新的剧情弧"}],
        "removeNodeIds": [],
    }


def test_merge_outline_patch_treats_an_existing_node_subset_as_a_safe_merge():
    from app.services.outline_candidate_service import merge_outline_patch

    current = _outline()
    merged = merge_outline_patch(current, {
        "mainSummary": "优化后的主线",
        "nodes": [{"id": "a-1", "summary": "新的剧情弧"}],
    })

    nodes = {node["id"]: node for node in merged["nodes"]}
    assert merged["mainSummary"] == "优化后的主线"
    assert nodes["a-1"]["summary"] == "新的剧情弧"
    assert nodes["ch-1"]["title"] == "第一章"


def _large_outline() -> dict:
    nodes = []
    chapter_order = 1
    for volume_order in range(1, 4):
        volume_id = f"v-{volume_order}"
        nodes.append({
            "id": volume_id, "type": "volume", "parentId": None,
            "title": f"第{volume_order}卷", "summary": f"第{volume_order}卷独立剧情", "order": volume_order,
        })
        for arc_order in range(1, 4):
            arc_id = f"a-{volume_order}-{arc_order}"
            nodes.append({
                "id": arc_id, "type": "arc", "parentId": volume_id,
                "title": f"第{volume_order}-{arc_order}弧", "summary": f"剧情弧{volume_order}-{arc_order}", "order": arc_order,
            })
            count = 23 if volume_order == 3 and arc_order == 3 else 22
            for _ in range(count):
                nodes.append({
                    "id": f"ch-{chapter_order}", "type": "chapter", "parentId": volume_id,
                    "arcId": arc_id, "title": f"第{chapter_order}章", "order": chapter_order,
                })
                chapter_order += 1
    return {"mainSummary": "跨越三卷的完整主线", "nodes": nodes}


def test_outline_edit_context_omits_untargeted_chapters_and_counts_arc_membership():
    from app.services.outline_candidate_service import build_outline_edit_context

    context = build_outline_edit_context(
        _large_outline(),
        instruction="请优化三卷各自的剧情弧",
        saved_chapter_ids={"ch-1"},
    )
    payload = json.loads(context)

    assert len(payload["nodes"]) == 12
    assert {node["type"] for node in payload["nodes"]} == {"volume", "arc"}
    first_arc = next(node for node in payload["nodes"] if node["id"] == "a-1-1")
    assert first_arc["chapterCount"] == 22
    assert first_arc["savedChapterCount"] == 1
    assert len(context) < 7000


def test_outline_edit_context_includes_only_target_chapter_and_neighbors():
    from app.services.outline_candidate_service import build_outline_edit_context

    payload = json.loads(build_outline_edit_context(
        _large_outline(),
        instruction="调整第100章的转折",
        saved_chapter_ids={"ch-100"},
    ))
    chapters = [node for node in payload["nodes"] if node["type"] == "chapter"]

    assert [node["id"] for node in chapters] == ["ch-99", "ch-100", "ch-101"]


def test_outline_edit_context_marks_legacy_flat_outline_shape():
    from app.services.outline_candidate_service import build_outline_edit_context

    payload = json.loads(build_outline_edit_context({
        "mainSummary": "旧版主线",
        "nodes": [{"id": "ch-1", "type": "chapter", "title": "第一章", "order": 1}],
    }, instruction="请优化大纲"))

    assert payload["outlineShape"] == "legacy_flat_chapters"


def test_outline_edit_context_rejects_whole_book_chapter_reorder():
    from app.services.outline_candidate_service import OutlinePatchError, build_outline_edit_context

    with pytest.raises(OutlinePatchError) as exc:
        build_outline_edit_context(
            _large_outline(),
            instruction="请重新安排所有章节并重排全部章节",
            saved_chapter_ids={"ch-1"},
        )

    assert exc.value.code == "outline_scope_too_broad"


def test_nested_volumes_and_arcs_normalize_to_existing_node_ids():
    from app.services.outline_candidate_service import normalize_model_outline_payload

    current = _outline()
    normalized = normalize_model_outline_payload({
        "mainSummary": "新的总纲",
        "volumes": [{
            "title": "第一卷",
            "summary": "新的卷简介",
            "arcs": [{"title": "失去", "summary": "新的完整剧情弧"}],
        }],
    }, current_outline=current)

    assert normalized == {
        "patchMode": "merge",
        "mainSummary": "新的总纲",
        "upsertNodes": [
            {"id": "v-1", "title": "第一卷", "summary": "新的卷简介"},
            {"id": "a-1", "title": "失去", "summary": "新的完整剧情弧"},
        ],
        "removeNodeIds": [],
    }


def test_nested_arc_title_match_must_be_unique_within_volume():
    from app.services.outline_candidate_service import OutlinePatchError, normalize_model_outline_payload

    current = _outline()
    current["nodes"].append({
        "id": "a-2", "type": "arc", "parentId": "v-1", "title": "失去", "order": 2,
    })

    with pytest.raises(OutlinePatchError) as exc:
        normalize_model_outline_payload({
            "volumes": [{"id": "v-1", "arcs": [{"title": "失去", "summary": "不应猜测"}]}],
        }, current_outline=current)

    assert exc.value.code == "node_reference_ambiguous"


def test_legacy_chapter_only_outline_can_be_validated_without_parent_volume():
    from app.services.outline_candidate_service import validate_outline_patch

    legacy = {
        "mainSummary": "旧版项目主线",
        "nodes": [
            {
                "id": "legacy-ch-1",
                "type": "chapter",
                "title": "第一章",
                "order": 1,
                "goal": "保留历史字段",
            },
            {
                "id": "legacy-ch-2",
                "type": "chapter",
                "title": "第二章",
                "order": 2,
            },
        ],
    }

    normalized = validate_outline_patch({"outline": legacy, "chapters": []}, legacy)

    assert [node["id"] for node in normalized["nodes"]] == ["legacy-ch-1", "legacy-ch-2"]
    assert normalized["nodes"][0]["goal"] == "保留历史字段"
