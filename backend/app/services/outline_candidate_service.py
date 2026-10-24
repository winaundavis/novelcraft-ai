"""Validation, stable hashing and safe diffs for project outline candidates."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


ALLOWED_OUTLINE_FIELDS = {"mainSummary", "nodes", "outlineVersion"}
ALLOWED_NODE_FIELDS = {
    "id",
    "parentId",
    "arcId",
    "type",
    "title",
    "summary",
    "conflict",
    "revelation",
    "emotion",
    "isKeyPoint",
    "order",
    "targetWordCount",
    "povCharacterId",
    # Legacy chapter-contract fields already stored by earlier outline versions.
    "characters",
    "events",
    "goal",
    "hook",
    "pacing",
    "stateChange",
}
NODE_TYPES = {"volume", "arc", "chapter"}


class OutlinePatchError(ValueError):
    """A safe, user-facing outline patch validation failure."""

    def __init__(self, message: str, code: str = "response_invalid") -> None:
        super().__init__(message)
        self.code = code


def _unwrap_outline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    current = payload
    for _ in range(3):
        wrapped = next(
            (
                current.get(key)
                for key in ("outline", "candidateOutline", "patch")
                if isinstance(current.get(key), dict)
            ),
            None,
        )
        if wrapped is None:
            break
        current = wrapped
    return current


def describe_model_outline_payload(payload: dict[str, Any]) -> tuple[str, int]:
    """Return a content-free shape label and structural node count."""
    if not isinstance(payload, dict):
        return "invalid", 0
    source = _unwrap_outline_payload(payload)
    replacements = source.get("arcReplacements") or source.get("replaceArcs")
    if isinstance(replacements, list):
        arc_count = sum(
            len(item.get("arcs") or [])
            for item in replacements
            if isinstance(item, dict) and isinstance(item.get("arcs"), list)
        )
        return "arc_replacements", arc_count
    if any(key in source for key in ("upsertNodes", "updates", "changes")):
        updates = source.get("upsertNodes") or source.get("updates") or source.get("changes") or []
        return "merge_patch", len(updates) if isinstance(updates, list) else 0
    if isinstance(source.get("nodes"), list):
        return "flat_nodes", len(source["nodes"])
    if isinstance(source.get("volumes"), list):
        volumes = source["volumes"]
        arc_count = sum(
            len(volume.get("arcs") or [])
            for volume in volumes
            if isinstance(volume, dict) and isinstance(volume.get("arcs") or [], list)
        )
        return "nested_volumes", len(volumes) + arc_count
    return "unknown", 0


def _match_existing_node(
    raw_node: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    node_type: str,
) -> dict[str, Any]:
    node_id = _text(raw_node.get("id"), 200)
    if node_id:
        matches = [node for node in candidates if _text(node.get("id"), 200) == node_id]
    else:
        title = _text(raw_node.get("title"), 500)
        matches = [node for node in candidates if title and _text(node.get("title"), 500) == title]
    if len(matches) > 1:
        raise OutlinePatchError(
            f"{node_type}无法唯一对应当前大纲。",
            code="node_reference_ambiguous",
        )
    if not matches:
        raise OutlinePatchError(
            f"{node_type}无法对应当前大纲。",
            code="node_reference_invalid",
        )
    return matches[0]


def _normalize_nested_outline_payload(
    payload: dict[str, Any],
    current_outline: dict[str, Any],
) -> dict[str, Any]:
    existing_nodes = [node for node in current_outline.get("nodes") or [] if isinstance(node, dict)]
    existing_volumes = [node for node in existing_nodes if node.get("type") == "volume"]
    raw_volumes = payload.get("volumes")
    if not isinstance(raw_volumes, list):
        raise OutlinePatchError("嵌套大纲缺少卷列表。", code="outline_schema_invalid")

    editable_fields = ("title", "summary", "conflict", "revelation", "emotion", "order")
    upserts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_volume in raw_volumes:
        if not isinstance(raw_volume, dict):
            raise OutlinePatchError("卷结构无效。", code="outline_schema_invalid")
        volume = _match_existing_node(raw_volume, existing_volumes, node_type="卷")
        volume_id = _text(volume.get("id"), 200)
        volume_patch = {"id": volume_id}
        volume_patch.update({key: deepcopy(raw_volume[key]) for key in editable_fields if key in raw_volume})
        if volume_id not in seen_ids:
            upserts.append(volume_patch)
            seen_ids.add(volume_id)

        raw_arcs = raw_volume.get("arcs", [])
        if not isinstance(raw_arcs, list):
            raise OutlinePatchError("剧情弧列表无效。", code="outline_schema_invalid")
        existing_arcs = [
            node for node in existing_nodes
            if node.get("type") == "arc" and _text(node.get("parentId"), 200) == volume_id
        ]
        for raw_arc in raw_arcs:
            if not isinstance(raw_arc, dict):
                raise OutlinePatchError("剧情弧结构无效。", code="outline_schema_invalid")
            arc = _match_existing_node(raw_arc, existing_arcs, node_type="剧情弧")
            arc_id = _text(arc.get("id"), 200)
            arc_patch = {"id": arc_id}
            arc_patch.update({key: deepcopy(raw_arc[key]) for key in editable_fields if key in raw_arc})
            if arc_id in seen_ids:
                raise OutlinePatchError("模型重复修改同一节点。", code="node_reference_ambiguous")
            upserts.append(arc_patch)
            seen_ids.add(arc_id)

    result: dict[str, Any] = {
        "patchMode": "merge",
        "upsertNodes": upserts,
        "removeNodeIds": [],
    }
    if "mainSummary" in payload:
        result["mainSummary"] = payload["mainSummary"]
    return result


def _generated_arc_id(
    volume_id: str,
    order: int,
    start: int,
    end: int,
    title: str,
    used_ids: set[str],
) -> str:
    seed = f"{volume_id}|{order}|{start}|{end}|{title}"
    for salt in range(100):
        suffix = hashlib.sha256(f"{seed}|{salt}".encode("utf-8")).hexdigest()[:12]
        node_id = f"generated-arc-{suffix}"
        if node_id not in used_ids:
            return node_id
    raise OutlinePatchError("无法为新剧情弧生成唯一 ID。", code="node_reference_ambiguous")


def _normalize_arc_replacements(
    payload: dict[str, Any],
    current_outline: dict[str, Any],
) -> dict[str, Any]:
    """Convert chapter-range arc plans into a complete server-owned outline."""
    raw_replacements = payload.get("arcReplacements") or payload.get("replaceArcs")
    if not isinstance(raw_replacements, list) or not raw_replacements:
        raise OutlinePatchError("剧情弧重划缺少有效计划。", code="outline_schema_invalid")

    current_nodes = [deepcopy(node) for node in current_outline.get("nodes") or [] if isinstance(node, dict)]
    volumes = [node for node in current_nodes if node.get("type") == "volume"]
    used_ids = {_text(node.get("id"), 200) for node in current_nodes if node.get("id")}
    replaced_volumes: set[str] = set()
    generated_arcs: list[dict[str, Any]] = []
    chapter_arc_updates: dict[str, str] = {}

    for raw_replacement in raw_replacements:
        if not isinstance(raw_replacement, dict):
            raise OutlinePatchError("剧情弧重划计划格式无效。", code="outline_schema_invalid")
        volume_ref = {
            "id": raw_replacement.get("volumeId") or raw_replacement.get("id"),
            "title": raw_replacement.get("volumeTitle") or raw_replacement.get("title"),
        }
        volume = _match_existing_node(volume_ref, volumes, node_type="卷")
        volume_id = _text(volume.get("id"), 200)
        if volume_id in replaced_volumes:
            raise OutlinePatchError("同一卷不能重复提交剧情弧重划计划。", code="node_reference_ambiguous")
        replaced_volumes.add(volume_id)

        chapters = sorted(
            (
                node for node in current_nodes
                if node.get("type") == "chapter" and _text(node.get("parentId"), 200) == volume_id
            ),
            key=lambda node: (_safe_int(node.get("order"), field="order"), _text(node.get("id"), 200)),
        )
        if not chapters:
            raise OutlinePatchError("目标卷没有可分配的章节。", code="arc_range_invalid")
        chapter_orders = {_safe_int(node.get("order"), field="order") for node in chapters}
        raw_arcs = raw_replacement.get("arcs")
        if not isinstance(raw_arcs, list) or not raw_arcs or len(raw_arcs) > 64:
            raise OutlinePatchError("剧情弧数量无效。", code="arc_range_invalid")

        covered_ids: set[str] = set()
        for arc_order, raw_arc in enumerate(raw_arcs, start=1):
            if not isinstance(raw_arc, dict):
                raise OutlinePatchError("剧情弧格式无效。", code="outline_schema_invalid")
            start = _safe_int(
                raw_arc.get("startChapter", raw_arc.get("startOrder", raw_arc.get("start"))),
                field="startChapter",
                minimum=1,
            )
            end = _safe_int(
                raw_arc.get("endChapter", raw_arc.get("endOrder", raw_arc.get("end"))),
                field="endChapter",
                minimum=1,
            )
            title = _text(raw_arc.get("title"), 500)
            summary = _text(raw_arc.get("summary"))
            expected_orders = set(range(start, end + 1)) if start <= end else set()
            selected = [node for node in chapters if _safe_int(node.get("order"), field="order") in expected_orders]
            selected_ids = {_text(node.get("id"), 200) for node in selected}
            if (
                not title
                or not summary
                or not expected_orders
                or not expected_orders.issubset(chapter_orders)
                or selected_ids & covered_ids
            ):
                raise OutlinePatchError("剧情弧章节范围必须有效、连续且不能重叠。", code="arc_range_invalid")

            node_id = _generated_arc_id(volume_id, arc_order, start, end, title, used_ids)
            used_ids.add(node_id)
            generated_arcs.append({
                "id": node_id,
                "parentId": volume_id,
                "type": "arc",
                "title": title,
                "summary": summary,
                "conflict": _text(raw_arc.get("conflict")),
                "revelation": _text(raw_arc.get("revelation")),
                "emotion": _text(raw_arc.get("emotion")),
                "isKeyPoint": bool(raw_arc.get("isKeyPoint", False)),
                "order": arc_order,
                "targetWordCount": sum(_safe_int(node.get("targetWordCount"), field="targetWordCount", minimum=0) for node in selected),
                "povCharacterId": None,
            })
            for chapter_id in selected_ids:
                chapter_arc_updates[chapter_id] = node_id
            covered_ids.update(selected_ids)

        all_chapter_ids = {_text(node.get("id"), 200) for node in chapters}
        if covered_ids != all_chapter_ids:
            raise OutlinePatchError("剧情弧计划必须覆盖目标卷的全部章节。", code="arc_range_invalid")

    result_nodes: list[dict[str, Any]] = []
    for node in current_nodes:
        if node.get("type") == "arc" and _text(node.get("parentId"), 200) in replaced_volumes:
            continue
        node_id = _text(node.get("id"), 200)
        if node_id in chapter_arc_updates:
            node["arcId"] = chapter_arc_updates[node_id]
        result_nodes.append(node)
    result_nodes.extend(generated_arcs)
    result = {
        "mainSummary": _text(payload.get("mainSummary")) if "mainSummary" in payload else _text(current_outline.get("mainSummary")),
        "nodes": result_nodes,
    }
    if "outlineVersion" in current_outline:
        result["outlineVersion"] = current_outline["outlineVersion"]
    return result


def normalize_model_outline_payload(
    payload: dict[str, Any],
    *,
    current_outline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a small set of common model wrappers into the patch contract."""
    if not isinstance(payload, dict):
        return payload
    payload = _unwrap_outline_payload(payload)

    if "arcReplacements" in payload or "replaceArcs" in payload:
        if current_outline is None:
            raise OutlinePatchError("剧情弧重划缺少当前大纲基线。", code="outline_schema_invalid")
        return _normalize_arc_replacements(payload, current_outline)

    if "volumes" in payload:
        if current_outline is None:
            raise OutlinePatchError("嵌套大纲缺少当前大纲基线。", code="outline_schema_invalid")
        return _normalize_nested_outline_payload(payload, current_outline)

    if "nodes" in payload:
        return payload

    raw_updates = payload.get("upsertNodes")
    if raw_updates is None:
        raw_updates = payload.get("updates")
    if raw_updates is None:
        raw_updates = payload.get("changes")
    if not isinstance(raw_updates, list):
        return payload

    upserts: list[dict[str, Any]] = []
    for raw_update in raw_updates:
        if not isinstance(raw_update, dict):
            raise OutlinePatchError("大纲补丁节点格式无效。")
        node_id = raw_update.get("id") or raw_update.get("nodeId")
        normalized = {
            key: deepcopy(value)
            for key, value in raw_update.items()
            if key in ALLOWED_NODE_FIELDS
        }
        if node_id:
            normalized["id"] = node_id
        upserts.append(normalized)

    result: dict[str, Any] = {
        "patchMode": "merge",
        "upsertNodes": upserts,
        "removeNodeIds": payload.get("removeNodeIds") or payload.get("deletedNodeIds") or [],
    }
    if "mainSummary" in payload:
        result["mainSummary"] = payload["mainSummary"]
    return result


def build_outline_edit_context(
    outline: dict[str, Any] | None,
    instruction: str = "",
    saved_chapter_ids: set[str] | None = None,
) -> str:
    """Return a compact, ID-preserving outline view for model patch requests."""
    source = outline if isinstance(outline, dict) else {}
    normalized_instruction = re.sub(r"\s+", "", instruction or "")
    whole_book_markers = ("所有章节", "全部章节", "全书章节", "整本章节")
    reorder_markers = ("重排", "重新安排", "重新排序", "重构")
    if any(marker in normalized_instruction for marker in whole_book_markers) and any(
        marker in normalized_instruction for marker in reorder_markers
    ):
        raise OutlinePatchError(
            "一次重排全书章节范围过大，请缩小到具体卷、剧情弧或章节。",
            code="outline_scope_too_broad",
        )

    saved_ids = {str(item) for item in (saved_chapter_ids or set()) if item}
    raw_nodes = [node for node in source.get("nodes") or [] if isinstance(node, dict)]
    chapters = sorted(
        (node for node in raw_nodes if node.get("type") == "chapter"),
        key=lambda node: (int(node.get("order") or 0), _text(node.get("id"), 200)),
    )
    arc_counts: dict[str, int] = {}
    saved_arc_counts: dict[str, int] = {}
    for chapter in chapters:
        arc_id = _text(chapter.get("arcId"), 200)
        if not arc_id:
            continue
        arc_counts[arc_id] = arc_counts.get(arc_id, 0) + 1
        if _text(chapter.get("id"), 200) in saved_ids:
            saved_arc_counts[arc_id] = saved_arc_counts.get(arc_id, 0) + 1

    volume_chapters: dict[str, list[dict[str, Any]]] = {}
    for chapter in chapters:
        volume_id = _text(chapter.get("parentId"), 200)
        if volume_id:
            volume_chapters.setdefault(volume_id, []).append(chapter)

    target_indexes: set[int] = set()
    requested_orders = {int(value) for value in re.findall(r"第\s*(\d+)\s*章", instruction or "")}
    for index, chapter in enumerate(chapters):
        chapter_id = _text(chapter.get("id"), 200)
        title = _text(chapter.get("title"), 500)
        order = int(chapter.get("order") or 0)
        if (chapter_id and chapter_id in instruction) or (title and title in instruction) or order in requested_orders:
            target_indexes.update(range(max(0, index - 1), min(len(chapters), index + 2)))
    selected_chapter_ids = {
        _text(chapters[index].get("id"), 200)
        for index in sorted(target_indexes)
    }

    compact_nodes: list[dict[str, Any]] = []
    for raw_node in raw_nodes:
        node_type = _text(raw_node.get("type"), 20)
        node_id = _text(raw_node.get("id"), 200)
        if node_type == "chapter" and node_id not in selected_chapter_ids:
            continue
        compact = {
            "id": node_id,
            "type": node_type,
            "parentId": raw_node.get("parentId"),
            "arcId": raw_node.get("arcId"),
            "title": _text(raw_node.get("title"), 500),
            "order": int(raw_node.get("order") or 0),
        }
        if node_type in {"volume", "arc"}:
            compact["summary"] = _text(raw_node.get("summary"), 360)
        if node_type == "volume":
            owned = volume_chapters.get(node_id, [])
            orders = [int(node.get("order") or 0) for node in owned]
            compact["chapterCount"] = len(owned)
            compact["chapterOrderStart"] = min(orders) if orders else 0
            compact["chapterOrderEnd"] = max(orders) if orders else 0
        if node_type == "arc":
            compact["chapterCount"] = arc_counts.get(node_id, 0)
            compact["savedChapterCount"] = saved_arc_counts.get(node_id, 0)
        compact_nodes.append({key: value for key, value in compact.items() if value not in (None, "")})
    payload = {
        "mainSummary": _text(source.get("mainSummary"), 1200),
        "outlineShape": (
            "legacy_flat_chapters"
            if raw_nodes and not any(node.get("type") in {"volume", "arc"} for node in raw_nodes)
            else "hierarchical"
        ),
        "nodes": compact_nodes,
    }
    # Reserve room for system instructions and the latest author command.
    # Never return an oversized structural fallback: IDs/titles alone can
    # exceed the cap after an author splits a book into many short arcs.
    max_chars = 9000
    for summary_limit in (360, 180, 100, 60):
        bounded_nodes = []
        for node in compact_nodes:
            copy = dict(node)
            if "summary" in copy:
                copy["summary"] = _text(copy.get("summary"), summary_limit)
                if not copy["summary"]:
                    copy.pop("summary", None)
            bounded_nodes.append(copy)
        payload["nodes"] = bounded_nodes
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= max_chars:
            return rendered

    # A columnar index removes repeated JSON keys before sacrificing story
    # detail. It is request-only; saved outlines and model patches stay in
    # their original schema. Grouping also avoids repeating every parent ID.
    arcs = [n for n in compact_nodes if n["type"] == "arc"]
    focus_ids = {
        n["id"] for n in compact_nodes
        if n["id"] in instruction or (n.get("title") and n["title"] in instruction)
    }
    target_arcs = focus_ids & {n["id"] for n in arcs}
    focus_volumes = focus_ids & {n["id"] for n in compact_nodes if n["type"] == "volume"}
    for node in compact_nodes:
        if node["type"] == "volume":
            order = int(node.get("order") or 0)
            chinese = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}.get(order)
            if f"第{order}卷" in normalized_instruction or (chinese and f"第{chinese}卷" in normalized_instruction):
                focus_volumes.add(node["id"])
        if node["type"] == "chapter" and node["id"] in selected_chapter_ids:
            target_arcs.add(str(node.get("arcId") or ""))
    if not target_arcs:
        target_arcs = {n["id"] for n in arcs if n.get("parentId") in focus_volumes}

    payload["mainSummary"] = _text(source.get("mainSummary"), 600)
    payload["nodes"] = [
        {**n, **({"summary": _text(n.get("summary"), 120)} if "summary" in n else {})}
        for n in compact_nodes if n["type"] != "arc"
    ]
    payload["arcFields"] = ["id", "title", "chapterCount", "savedChapterCount", "summary"]
    payload["contextEncoding"] = "arcGroups.rows columns follow arcFields; parentId is volumeId; omitted nodes remain unchanged"
    for target_limit, other_limit in ((180, 60), (100, 30), (60, 15), (40, 0)):
        groups: dict[str, list[list[Any]]] = {}
        for node in sorted(arcs, key=lambda n: int(n.get("order") or 0)):
            summary_limit = target_limit if node["id"] in target_arcs else other_limit
            groups.setdefault(str(node.get("parentId") or ""), []).append([
                node["id"], _text(node.get("title"), 80),
                node.get("chapterCount", 0), node.get("savedChapterCount", 0),
                _text(node.get("summary"), summary_limit),
            ])
        payload["arcGroups"] = [{"volumeId": key, "rows": rows} for key, rows in groups.items()]
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= max_chars:
            return rendered

    # Very large books keep all volume context but expand only the requested
    # arcs/volume. Do not guess a subset when the author has not named one.
    if target_arcs:
        payload["omittedArcCount"] = len(arcs) - len(target_arcs)
        for group in payload["arcGroups"]:
            group["rows"] = [row for row in group["rows"] if row[0] in target_arcs]
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= max_chars:
            return rendered
    raise OutlinePatchError(
        "本次编辑范围过大，请指定要修改的卷、剧情弧或章节；当前大纲和对话均已保留。",
        code="outline_scope_too_broad",
    )


def merge_outline_patch(current_outline: dict[str, Any], model_payload: dict[str, Any]) -> dict[str, Any]:
    """Merge an explicit model patch into a full outline before validation."""
    current_nodes = [
        node for node in (current_outline.get("nodes") or [])
        if isinstance(node, dict)
    ]
    returned_nodes = model_payload.get("nodes")
    if model_payload.get("patchMode") != "merge" and isinstance(returned_nodes, list):
        current_ids = {_text(node.get("id"), 200) for node in current_nodes if node.get("id")}
        returned_ids = [
            _text(node.get("id"), 200)
            for node in returned_nodes
            if isinstance(node, dict)
        ]
        is_existing_subset = (
            0 < len(returned_nodes) < len(current_nodes)
            and len(returned_ids) == len(returned_nodes)
            and all(node_id in current_ids for node_id in returned_ids)
        )
        if is_existing_subset:
            model_payload = {
                "patchMode": "merge",
                "upsertNodes": [
                    {key: deepcopy(value) for key, value in node.items() if key in ALLOWED_NODE_FIELDS}
                    for node in returned_nodes
                ],
                "removeNodeIds": [],
                **({"mainSummary": model_payload["mainSummary"]} if "mainSummary" in model_payload else {}),
            }
    if model_payload.get("patchMode") != "merge":
        return model_payload

    allowed_patch_fields = {"patchMode", "mainSummary", "upsertNodes", "removeNodeIds"}
    if set(model_payload) - allowed_patch_fields:
        raise OutlinePatchError("大纲补丁包含不允许修改的字段。")
    upserts = model_payload.get("upsertNodes", [])
    remove_ids = model_payload.get("removeNodeIds", [])
    if not isinstance(upserts, list) or not isinstance(remove_ids, list):
        raise OutlinePatchError("大纲补丁的节点变更格式无效。")

    result = deepcopy(current_outline if isinstance(current_outline, dict) else {})
    nodes = [deepcopy(node) for node in result.get("nodes") or [] if isinstance(node, dict)]
    index_by_id = {_text(node.get("id"), 200): index for index, node in enumerate(nodes) if node.get("id")}
    seen_upserts: set[str] = set()
    for raw_patch in upserts:
        if not isinstance(raw_patch, dict):
            raise OutlinePatchError("大纲补丁节点格式无效。")
        node_id = _text(raw_patch.get("id"), 200)
        if not node_id or node_id in seen_upserts:
            raise OutlinePatchError("大纲补丁节点缺少 ID 或 ID 重复。")
        seen_upserts.add(node_id)
        if node_id not in index_by_id:
            raise OutlinePatchError(
                "大纲补丁引用了当前项目中不存在的节点。",
                code="node_reference_invalid",
            )
        nodes[index_by_id[node_id]].update(deepcopy(raw_patch))

    normalized_remove_ids = [_text(node_id, 200) for node_id in remove_ids]
    if any(not node_id for node_id in normalized_remove_ids):
        raise OutlinePatchError("大纲补丁包含无效的删除节点 ID。")
    if len(set(normalized_remove_ids)) != len(normalized_remove_ids):
        raise OutlinePatchError("大纲补丁包含重复的删除节点 ID。")
    remove_set = set(normalized_remove_ids)
    if remove_set - set(index_by_id):
        raise OutlinePatchError(
            "大纲补丁引用了当前项目中不存在的节点。",
            code="node_reference_invalid",
        )
    if remove_set & seen_upserts:
        raise OutlinePatchError("同一大纲节点不能同时修改和删除。")

    merged = {
        "mainSummary": _text(model_payload.get("mainSummary"))
        if "mainSummary" in model_payload
        else _text(result.get("mainSummary")),
        "nodes": [node for node in nodes if _text(node.get("id"), 200) not in remove_set],
    }
    if "outlineVersion" in result:
        merged["outlineVersion"] = result["outlineVersion"]
    return merged


def outline_hash(outline: dict[str, Any] | None) -> str:
    canonical = json.dumps(
        outline if isinstance(outline, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text(value: Any, limit: int = 10000) -> str:
    return str(value or "").strip()[:limit]


def _safe_int(value: Any, *, field: str, default: int = 0, minimum: int | None = None) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError) as exc:
        raise OutlinePatchError(
            f"大纲字段 {field} 不是有效数字。",
            code="outline_field_invalid",
        ) from exc
    return max(minimum, parsed) if minimum is not None else parsed


def protected_saved_chapter_removals(
    project: dict[str, Any],
    candidate_outline: dict[str, Any],
) -> list[dict[str, str]]:
    candidate_ids = {
        _text(node.get("id"), 200)
        for node in candidate_outline.get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    }
    current_by_id = {
        _text(node.get("id"), 200): node
        for node in (project.get("outline") or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    }
    removals: list[dict[str, str]] = []
    for chapter in project.get("chapters") or []:
        if not isinstance(chapter, dict) or not _text(chapter.get("content")):
            continue
        chapter_id = _text(chapter.get("id"), 200)
        # A legacy chapter row whose ID was never present in the outline cannot
        # be removed by this candidate. Treating it as a removal creates a
        # false confirmation warning for every otherwise unrelated outline edit.
        if chapter_id not in current_by_id:
            continue
        if chapter_id and chapter_id not in candidate_ids:
            current = current_by_id.get(chapter_id) or {}
            removals.append({
                "id": chapter_id,
                "title": _text(current.get("title") or chapter.get("title"), 500),
            })
    return removals


def validate_outline_patch(
    project: dict[str, Any],
    candidate_outline: dict[str, Any],
    *,
    allow_saved_chapter_removal: bool = False,
) -> dict[str, Any]:
    if not isinstance(candidate_outline, dict):
        raise OutlinePatchError("大纲候选必须是 JSON 对象。", code="outline_schema_invalid")
    unknown_outline = set(candidate_outline) - ALLOWED_OUTLINE_FIELDS
    if unknown_outline:
        raise OutlinePatchError("大纲候选包含不允许修改的字段。", code="outline_schema_invalid")

    raw_nodes = candidate_outline.get("nodes")
    if not isinstance(raw_nodes, list):
        raise OutlinePatchError("大纲候选缺少有效的节点列表。", code="outline_schema_invalid")

    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise OutlinePatchError("大纲节点格式无效。", code="outline_schema_invalid")
        if set(raw_node) - ALLOWED_NODE_FIELDS:
            raise OutlinePatchError("大纲节点包含不允许修改的字段。", code="outline_schema_invalid")
        node_id = _text(raw_node.get("id"), 200)
        node_type = _text(raw_node.get("type"), 20)
        if not node_id or node_type not in NODE_TYPES:
            raise OutlinePatchError("大纲节点缺少有效的 id 或 type。", code="outline_schema_invalid")
        if node_id in seen_ids:
            raise OutlinePatchError(f"大纲节点 ID 重复：{node_id}", code="node_reference_ambiguous")
        seen_ids.add(node_id)

        node: dict[str, Any] = {
            "id": node_id,
            "parentId": _text(raw_node.get("parentId"), 200) or None,
            "type": node_type,
            "title": _text(raw_node.get("title"), 500),
            "summary": _text(raw_node.get("summary")),
            "conflict": _text(raw_node.get("conflict")),
            "revelation": _text(raw_node.get("revelation")),
            "emotion": _text(raw_node.get("emotion")),
            "isKeyPoint": bool(raw_node.get("isKeyPoint", False)),
            "order": _safe_int(raw_node.get("order"), field="order"),
            "targetWordCount": _safe_int(raw_node.get("targetWordCount"), field="targetWordCount", minimum=0),
            "povCharacterId": _text(raw_node.get("povCharacterId"), 200) or None,
        }
        for legacy_field in ("characters", "events", "goal", "hook", "pacing", "stateChange"):
            if legacy_field in raw_node:
                node[legacy_field] = deepcopy(raw_node[legacy_field])
        if node_type == "chapter":
            node["arcId"] = _text(raw_node.get("arcId"), 200) or None
        nodes.append(node)

    by_id = {node["id"]: node for node in nodes}
    # Projects created before volume/arc hierarchy was introduced can contain
    # a flat chapter-only outline.  Preserve that historical shape instead of
    # rejecting an otherwise safe summary-only candidate.
    legacy_flat_outline = bool(nodes) and not any(
        node["type"] in {"volume", "arc"} for node in nodes
    )
    for node in nodes:
        if node["type"] == "volume":
            if node["parentId"] is not None:
                raise OutlinePatchError(f"卷节点 {node['id']} 不应设置父级。", code="outline_parent_invalid")
            continue
        if legacy_flat_outline and node["type"] == "chapter" and node["parentId"] is None and not node.get("arcId"):
            continue
        parent = by_id.get(node["parentId"] or "")
        if parent is None or parent["type"] != "volume":
            raise OutlinePatchError(f"节点 {node['id']} 的父级卷不存在。", code="outline_parent_invalid")
        if node["type"] == "chapter" and node.get("arcId"):
            arc = by_id.get(node["arcId"])
            if arc is None or arc["type"] != "arc" or arc["parentId"] != node["parentId"]:
                raise OutlinePatchError(f"章节 {node['id']} 的剧情弧与父级卷不一致。", code="outline_parent_invalid")

    candidate_ids = set(by_id)
    current_nodes = [
        node for node in (project.get("outline") or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    ]
    current_by_id = {_text(node.get("id"), 200): node for node in current_nodes}
    new_ids = candidate_ids - set(current_by_id)
    existing_volume_ids = {
        node_id for node_id, node in current_by_id.items() if node.get("type") == "volume"
    }
    invalid_new_ids = {
        node_id for node_id in new_ids
        if by_id[node_id]["type"] != "arc"
        or not node_id.startswith("generated-arc-")
        or _text(by_id[node_id].get("parentId"), 200) not in existing_volume_ids
    }
    if current_by_id and invalid_new_ids:
        raise OutlinePatchError(
            "大纲候选包含当前项目中不存在的节点。",
            code="node_reference_invalid",
        )
    for chapter in project.get("chapters") or []:
        if not isinstance(chapter, dict) or not _text(chapter.get("content")):
            continue
        chapter_id = _text(chapter.get("id"), 200)
        if chapter_id not in current_by_id:
            continue
        candidate_node = by_id.get(chapter_id)
        if chapter_id not in candidate_ids or not candidate_node or candidate_node["type"] != "chapter":
            if allow_saved_chapter_removal:
                continue
            raise OutlinePatchError(f"已有正文的章节 {chapter_id} 不能从大纲中删除。", code="saved_chapter_delete_forbidden")
        current_node = current_by_id.get(chapter_id)
        if current_node and _text(candidate_node.get("parentId"), 200) != _text(current_node.get("parentId"), 200):
            raise OutlinePatchError(
                f"已有正文的章节 {chapter_id} 不能跨卷移动。",
                code="saved_chapter_move_forbidden",
            )

    normalized: dict[str, Any] = {
        "mainSummary": _text(candidate_outline.get("mainSummary")),
        "nodes": nodes,
    }
    if "outlineVersion" in candidate_outline:
        normalized["outlineVersion"] = _safe_int(candidate_outline.get("outlineVersion"), field="outlineVersion", default=1, minimum=1)
    return normalized


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "title": node.get("title") or "",
        "parentId": node.get("parentId"),
        "arcId": node.get("arcId"),
        "order": int(node.get("order") or 0),
    }


def build_outline_diff(previous_outline: dict[str, Any], candidate_outline: dict[str, Any]) -> dict[str, Any]:
    previous_nodes = {
        str(node.get("id")): deepcopy(node)
        for node in previous_outline.get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    }
    candidate_nodes = {
        str(node.get("id")): deepcopy(node)
        for node in candidate_outline.get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    }

    added = [_node_summary(candidate_nodes[node_id]) for node_id in candidate_nodes.keys() - previous_nodes.keys()]
    removed = [_node_summary(previous_nodes[node_id]) for node_id in previous_nodes.keys() - candidate_nodes.keys()]
    changed: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    movement_fields = {"parentId", "arcId", "order"}
    for node_id in previous_nodes.keys() & candidate_nodes.keys():
        before = previous_nodes[node_id]
        after = candidate_nodes[node_id]
        changed_fields = sorted(
            field for field in ALLOWED_NODE_FIELDS
            if field not in {"id"} and before.get(field) != after.get(field)
        )
        if changed_fields:
            changed.append({"id": node_id, "type": after.get("type"), "title": after.get("title") or "", "fields": changed_fields})
        if any(before.get(field) != after.get(field) for field in movement_fields):
            moved.append({
                "id": node_id,
                "type": after.get("type"),
                "title": after.get("title") or "",
                "from": {field: before.get(field) for field in sorted(movement_fields)},
                "to": {field: after.get(field) for field in sorted(movement_fields)},
            })

    key = lambda item: (str(item.get("type") or ""), int(item.get("order") or 0), str(item.get("id") or ""))
    return {
        "mainSummaryChanged": (previous_outline.get("mainSummary") or "") != (candidate_outline.get("mainSummary") or ""),
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "changed": sorted(changed, key=lambda item: str(item["id"])),
        "moved": sorted(moved, key=lambda item: str(item["id"])),
    }
