"""Frozen style plans and an append-only completion ledger in existing JSON."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.dao import style_dao
from app.db import default_style_config, get_conn
from app.models.schemas import gen_id

PACE_CONFIG = {
    "fast": {"target_chapters": 9, "total_steps": 3, "interval_chapters": 3, "label": "快速（9 章完成）"},
    "medium": {"target_chapters": 60, "total_steps": 10, "interval_chapters": 6, "label": "中速（60 章完成）"},
    "slow": {"target_chapters": 200, "total_steps": 20, "interval_chapters": 10, "label": "慢速（200 章完成）"},
}
VALID_PACES = {*PACE_CONFIG, "custom"}
VALID_PRESETS = {"concise", "poetic", "humorous", "intense", "tender", "epic", "lightNovel", "western", "emotional_resonance"}
DEFAULT_PACE = "medium"


class TransitionRevisionConflict(ValueError):
    status_code = 409
    code = "style_plan_conflict"

    def __init__(self):
        super().__init__("风格过渡计划或进度已更新，正文候选已保留且未覆盖正文；请基于最新计划重新生成。")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _has_table(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _on_conn(conn: Any, project_id: str) -> dict | None:
    # Old isolated fixtures predate styles; never swallow actual SQL faults.
    if not _has_table(conn, "style_transitions"):
        return None
    row = conn.execute("SELECT * FROM style_transitions WHERE project_id=?", (project_id,)).fetchone()
    return style_dao.row_to_transition(row) if row else None


def _is_v2(transition: dict | None) -> bool:
    return bool(transition and (transition.get("plan") or {}).get("schemaVersion") == 2)


def _present(transition: dict | None) -> dict | None:
    if not transition:
        return None
    if not _is_v2(transition):
        return {**transition, "schemaVersion": 1, "legacyReadOnly": True}
    plan = transition["plan"]
    count = len(plan["completedChapters"])
    target = plan["targetChapters"]
    return {**transition, "schemaVersion": 2, "legacyReadOnly": False,
            "planId": plan["planId"], "revision": plan["revision"],
            "targetChapters": target, "completedCount": count,
            "progress": min(count / target, 1.0),
            "sourceSnapshot": plan["sourceSnapshot"], "targetSnapshot": plan["targetSnapshot"]}


def count_completed_chapters(project_id: str) -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM chapters WHERE project_id=? AND TRIM(content) != ''", (project_id,)).fetchone()[0])


def start_transition(project_id: str, pace: str, *, target_chapters: int | None = None,
                     target_style: dict | None = None) -> dict:
    if pace not in VALID_PACES:
        raise ValueError("不支持的过渡档位")
    if target_chapters is None:
        if pace == "custom":
            raise ValueError("自定义过渡需要 targetChapters")
        target_chapters = PACE_CONFIG[pace]["target_chapters"]
    if type(target_chapters) is not int or not 1 <= target_chapters <= 200:
        raise ValueError("targetChapters 必须是 1 到 200 之间的整数")
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute("SELECT style_config FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("项目未找到")
        current_style = json.loads(project["style_config"] or "{}")
        current_key = current_style.get("presetKey") or next(
            (key for key in VALID_PRESETS if default_style_config(key)["name"] == current_style.get("name")),
            "",
        )
        if target_style is not None:
            requested_key = target_style.get("presetKey") or ""
            if requested_key:
                if requested_key not in VALID_PRESETS:
                    raise ValueError("请选择有效的目标文风预设")
                key = requested_key
                preset = default_style_config(key)
            else:
                if current_key not in VALID_PRESETS:
                    raise ValueError("项目当前文风无法识别，请先选择一个目标文风预设")
                key = current_key
                preset = dict(current_style)
            custom = target_style.get("customPrompt") or ""
        else:
            preset = dict(current_style)
            key = current_key
            custom = preset.get("customPrompt") or ""
        if not isinstance(custom, str) or len(custom) > 2000:
            raise ValueError("文字补充不能超过 2000 字")
        preset.pop("id", None)
        preset.pop("customPrompt", None)
        binding = conn.execute("SELECT profile_id FROM style_bindings WHERE project_id=?", (project_id,)).fetchone()
        row = conn.execute("SELECT * FROM style_profiles WHERE id=? AND project_id=?", (binding[0], project_id)).fetchone() if binding else None
        if row is None:
            row = conn.execute("SELECT * FROM style_profiles WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (project_id,)).fetchone()
        profile = style_dao.row_to_profile(row) if row else {}
        source = {"profileId": profile.get("id", ""), "profileVersion": profile.get("version", 1),
                  "name": profile.get("name", "原稿文风"), "status": profile.get("status", "unavailable"),
                  "dimensions": profile.get("dimensions") or {},
                  "identity": profile.get("identity") or {}, "mechanics": profile.get("mechanics") or {},
                  "boundaries": profile.get("boundaries") or {},
                  "rules": {k: profile.get(v) or [] for k, v in {
                      "narrative_rules": "narrativeRules", "character_rules": "characterRules",
                      "language_rules": "languageRules", "rhythm_rules": "rhythmRules", "anti_ai_rules": "antiAiRules"}.items()}}
        baseline = [r[0] for r in conn.execute("SELECT id FROM chapters WHERE project_id=? AND TRIM(content) != '' ORDER BY id", (project_id,))]
        excluded = set()
        for table, column in (("chapter_commit_history", "chapter_id"), ("chapter_trash", "source_chapter_id")):
            if _has_table(conn, table):
                excluded.update(r[0] for r in conn.execute(f"SELECT DISTINCT {column} FROM {table} WHERE project_id=?", (project_id,)))
        plan = {"schemaVersion": 2, "planId": gen_id(), "revision": 1,
                "sourceSnapshot": source, "targetSnapshot": {"presetKey": key, "preset": preset, "customPrompt": custom},
                "targetChapters": target_chapters, "baselineChapterIds": baseline,
                "excludedChapterIds": sorted(excluded - set(baseline)), "completedChapters": []}
        config = PACE_CONFIG.get(pace, {"total_steps": target_chapters, "interval_chapters": 1})
        now = _utc_now()
        conn.execute("""INSERT INTO style_transitions
            (id,project_id,profile_id,pace,progress,interval_chapters,total_steps,plan,started_at,updated_at)
            VALUES (?,?,?,?,0,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
            profile_id=excluded.profile_id,pace=excluded.pace,progress=0,interval_chapters=excluded.interval_chapters,
            total_steps=excluded.total_steps,plan=excluded.plan,started_at=excluded.started_at,updated_at=excluded.updated_at""",
            (gen_id(), project_id, source["profileId"], pace, config["interval_chapters"], config["total_steps"],
             json.dumps(plan, ensure_ascii=False), now, now))
        return _present(_on_conn(conn, project_id))


def get_transition(project_id: str) -> dict | None:
    with get_conn() as conn:
        return _present(_on_conn(conn, project_id))


def get_transition_progress(project_id: str) -> dict | None:
    return get_transition(project_id)


def advance_transition(project_id: str) -> dict | None:
    """Compatibility read adapter; completion requires a chapter transaction."""
    return get_transition(project_id)


def plan_token(contract: dict) -> dict:
    transition = contract.get("transition") or {}
    return {"stylePlanId": transition.get("planId") or "", "stylePlanRevision": transition.get("revision") or 0}


def validate_plan_on_conn(conn: Any, project_id: str, plan_id: str | None, expected_revision: int | None,
                          *, chapter_id: str = "", allow_completed_revision: bool = False) -> dict | None:
    transition = _on_conn(conn, project_id)
    plan = (transition or {}).get("plan") or {}
    current_id = plan.get("planId", "") if _is_v2(transition) else ""
    current_revision = plan.get("revision", 0) if current_id else 0
    if plan_id is None and expected_revision is None:
        return transition  # ordinary manual edit / legacy caller
    if plan_id is None or expected_revision is None or plan_id != current_id:
        raise TransitionRevisionConflict()
    known_ids = {*plan.get("baselineChapterIds", []), *plan.get("excludedChapterIds", []),
                 *(x["chapterId"] for x in plan.get("completedChapters", []))}
    if expected_revision != current_revision and not (allow_completed_revision and chapter_id in known_ids):
        raise TransitionRevisionConflict()
    return transition


def validate_generation_token(project_id: str, plan_id: str | None, revision: int | None) -> None:
    with get_conn() as conn:
        validate_plan_on_conn(conn, project_id, plan_id, revision)


def record_chapter_completion_on_conn(conn: Any, project_id: str, chapter_id: str, plan_id: str | None,
                                     expected_revision: int | None, commit_id: str | None = None,
                                     source: str = "generation") -> dict | None:
    transition = validate_plan_on_conn(conn, project_id, plan_id, expected_revision,
                                      chapter_id=chapter_id, allow_completed_revision=True)
    if not _is_v2(transition):
        return _present(transition)
    plan = transition["plan"]
    chapter = conn.execute("SELECT content FROM chapters WHERE id=? AND project_id=?", (chapter_id, project_id)).fetchone()
    if not chapter or not (chapter[0] or "").strip():
        return _present(transition)
    known = {*plan["baselineChapterIds"], *plan["excludedChapterIds"], *(x["chapterId"] for x in plan["completedChapters"])}
    if chapter_id in known:
        return _present(transition)
    if source in {"import", "restore"}:
        exclude_chapters_on_conn(conn, project_id, [chapter_id])
        return _present(_on_conn(conn, project_id))
    weight = min((len(plan["completedChapters"]) + 1) / plan["targetChapters"], 1.0)
    plan["completedChapters"].append({"chapterId": chapter_id, "weight": weight, "commitId": commit_id or ""})
    plan["revision"] += 1
    conn.execute("UPDATE style_transitions SET plan=?,progress=?,updated_at=? WHERE project_id=?",
                 (json.dumps(plan, ensure_ascii=False), weight, _utc_now(), project_id))
    return _present(transition)


def exclude_chapters_on_conn(conn: Any, project_id: str, chapter_ids: list[str] | set[str]) -> None:
    transition = _on_conn(conn, project_id)
    if not _is_v2(transition):
        return
    plan = transition["plan"]
    excluded = set(plan["excludedChapterIds"])
    updated = excluded | {x for x in chapter_ids if x}
    if updated == excluded:
        return
    plan["excludedChapterIds"] = sorted(updated)
    plan["revision"] += 1
    conn.execute("UPDATE style_transitions SET plan=?,updated_at=? WHERE project_id=?",
                 (json.dumps(plan, ensure_ascii=False), _utc_now(), project_id))


def build_transition_block(transition: dict, profile: dict | None, style_config: dict | None) -> dict:
    plan = transition.get("plan") or {}
    if not _is_v2(transition):
        return {"pace": transition["pace"], "progress": plan.get("weight", 0), "weight": plan.get("weight", 0),
                "step": plan.get("step", 0), "totalSteps": transition["totalSteps"],
                "intervalChapters": transition["intervalChapters"], "legacyReadOnly": True,
                "instruction": build_transition_instruction(transition, profile, style_config)}
    result = _present(transition)
    weight = min((result["completedCount"] + 1) / result["targetChapters"], 1.0)
    return {k: v for k, v in {**result, "weight": weight, "step": result["completedCount"],
            "instruction": build_transition_instruction(transition, profile, style_config)}.items() if k != "plan"}


def build_transition_instruction(transition: dict, profile: dict | None, style_config: dict | None) -> str:
    plan = transition.get("plan") or {}
    target = plan.get("targetSnapshot") or {}
    preset = target.get("preset") or style_config or {}
    if _is_v2(transition):
        count, total = len(plan["completedChapters"]), plan["targetChapters"]
        weight = min((count + 1) / total, 1.0)
        lines = [f"计划进度 {count}/{total} 章；本次目标权重 {weight:.2%}，原稿权重 {1-weight:.2%}。"]
    else:
        weight = float(plan.get("weight", 0))
        lines = ["旧版过渡计划仅供读取；重新设置后启用按新章登记的计划。"]
    lines.append(f"目标预设：{preset.get('name') or '项目当前风格'}。")
    if weight <= 0.34:
        lines.append("保留原稿风格与写法机制为主，轻微引入目标预设及补充约束。")
    elif weight <= 0.67:
        lines.append("过渡中段：原稿机制与目标机制并行，按权重逐步增强目标约束。")
    else:
        lines.append("以目标预设与文字补充为主，保留必要的原稿辨识度。")
    if target.get("customPrompt"):
        lines.append("目标文字补充（按本次目标权重执行）：" + target["customPrompt"])
    lines.append("故事事实、人物设定、当前大纲、时间线始终优先于文风；不得借文风调整改变剧情。")
    return "\n".join(lines)
