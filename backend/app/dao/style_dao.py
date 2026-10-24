"""M4 文风画像引擎 · 数据访问层（DAO）。

封装 style_profiles / style_bindings / style_transitions 三张表的全部 SQL 操作：
- style_profiles：画像本体（四组规则 + 反 AI 规则 + 客观指标 + 五维 + 优缺点 + 报告）
- style_bindings：画像-项目绑定（一个项目当前生效一份画像）
- style_transitions：渐进过渡进度（快/中/慢三档，随项目持久化，中断可从当前档位恢复）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db import get_conn, json_dump
from app.models.schemas import gen_id
from app.utils.text_repair import repair_data


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        data = repair_data(__import__("json").loads(value))
        return data
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════
# row 转 dict
# ══════════════════════════════════════════════════════

def row_to_profile(row: Any) -> dict[str, Any]:
    available = set(row.keys()) if hasattr(row, "keys") else set()
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "name": row["name"],
        "sourceType": row["source_type"],
        "status": row["status"],
        "narrativeRules": _json_load(row["narrative_rules"], []),
        "characterRules": _json_load(row["character_rules"], []),
        "languageRules": _json_load(row["language_rules"], []),
        "rhythmRules": _json_load(row["rhythm_rules"], []),
        "antiAiRules": _json_load(row["anti_ai_rules"], []),
        "metrics": _json_load(row["metrics"], {}),
        "dimensions": _json_load(row["dimensions"], {}),
        "strengths": _json_load(row["strengths"], []),
        "weaknesses": _json_load(row["weaknesses"], []),
        "transitionPlan": _json_load(row["transition_plan"], {}),
        "report": row["report"] or "",
        "sampleCount": row["sample_count"],
        "sampleChars": row["sample_chars"],
        "llmUsed": bool(row["llm_used"]),
        "styleKind": row["style_kind"] if "style_kind" in available else "reference",
        "identity": _json_load(row["identity"], {}) if "identity" in available else {},
        "mechanics": _json_load(row["mechanics"], {}) if "mechanics" in available else {},
        "boundaries": _json_load(row["boundaries"], {}) if "boundaries" in available else {},
        "version": int(row["version"] or 1) if "version" in available else 1,
        "parentId": row["parent_id"] if "parent_id" in available else "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_to_transition(row: Any) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "projectId": row["project_id"],
        "profileId": row["profile_id"],
        "pace": row["pace"],
        "progress": row["progress"],
        "intervalChapters": row["interval_chapters"],
        "totalSteps": row["total_steps"],
        "plan": _json_load(row["plan"], {}),
        "startedAt": row["started_at"],
        "updatedAt": row["updated_at"],
    }
    plan = result["plan"] or {}
    if int(plan.get("schemaVersion", 1) or 1) >= 2:
        completed = list(plan.get("completedChapters") or [])
        target = max(int(plan.get("targetChapters") or 1), 1)
        result.update({
            "completedCount": len(completed),
            "targetChapters": target,
            "planId": plan.get("planId") or "",
            "revision": int(plan.get("revision") or 1),
            "targetSnapshot": plan.get("targetSnapshot") or {},
            "sourceSnapshot": plan.get("sourceSnapshot") or {},
        })
    return result


# ══════════════════════════════════════════════════════
# style_profiles
# ══════════════════════════════════════════════════════

def create_profile(fields: dict[str, Any]) -> dict[str, Any]:
    """插入一份画像并返回（snake_case 键的字段已由调用方序列化 JSON）。"""
    profile_id = fields.get("id") or gen_id()
    today = fields.get("today", "")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO style_profiles (
                id, project_id, name, source_type, status,
                narrative_rules, character_rules, language_rules, rhythm_rules,
                anti_ai_rules, metrics, dimensions, strengths, weaknesses,
                transition_plan, report, sample_count, sample_chars, llm_used,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                fields["project_id"],
                fields.get("name", ""),
                fields.get("source_type", "import"),
                fields.get("status", "completed"),
                json_dump(fields.get("narrative_rules", [])),
                json_dump(fields.get("character_rules", [])),
                json_dump(fields.get("language_rules", [])),
                json_dump(fields.get("rhythm_rules", [])),
                json_dump(fields.get("anti_ai_rules", [])),
                json_dump(fields.get("metrics", {})),
                json_dump(fields.get("dimensions", {})),
                json_dump(fields.get("strengths", [])),
                json_dump(fields.get("weaknesses", [])),
                json_dump(fields.get("transition_plan", {})),
                fields.get("report", ""),
                fields.get("sample_count", 0),
                fields.get("sample_chars", 0),
                int(bool(fields.get("llm_used", 0))),
                today,
                today,
            ),
        )
        # Older isolated test databases may not carry P0-D columns; the base
        # profile remains valid there and the migration path fills them later.
        try:
            conn.execute(
                """UPDATE style_profiles SET style_kind = ?, identity = ?, mechanics = ?,
                   boundaries = ?, version = ?, parent_id = ? WHERE id = ?""",
                (
                    fields.get("style_kind", "reference"),
                    json_dump(fields.get("identity", {})),
                    json_dump(fields.get("mechanics", {})),
                    json_dump(fields.get("boundaries", {})),
                    int(fields.get("version", 1)),
                    fields.get("parent_id", ""),
                    profile_id,
                ),
            )
        except Exception:  # noqa: BLE001 - compatibility with pre-P0-D fixtures
            pass
    profile = get_profile(profile_id)
    # 不用 assert 表达“写入必须成功”：`python -O` 会剥掉 assert，届时这里会
    # 静默返回 None，调用方在别处炸掉且现场信息全丢。显式抛错反而更好定位。
    if profile is None:
        raise RuntimeError(f"风格画像写入后读取失败：{profile_id}")
    return profile


def get_profile(profile_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM style_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
    return row_to_profile(row) if row else None


def get_profile_by_project(project_id: str) -> dict[str, Any] | None:
    """返回项目最新一份画像（按创建时间倒序）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM style_profiles WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    return row_to_profile(row) if row else None


def list_profiles(project_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM style_profiles WHERE project_id = ? ORDER BY created_at DESC, id DESC",
            (project_id,),
        ).fetchall()
    return [row_to_profile(row) for row in rows]


def duplicate_profile(profile_id: str, *, name: str = "") -> dict[str, Any] | None:
    source = get_profile(profile_id)
    if not source:
        return None
    return create_profile(
        {
            "project_id": source["projectId"],
            "name": name.strip()[:120] or f"{source['name']} 副本",
            "source_type": source.get("sourceType", "import"),
            "style_kind": source.get("styleKind", "reference"),
            "identity": source.get("identity") or {},
            "mechanics": source.get("mechanics") or {},
            "boundaries": source.get("boundaries") or {},
            "version": int(source.get("version") or 1) + 1,
            "parent_id": source["id"],
            "status": source.get("status", "completed"),
            "narrative_rules": source.get("narrativeRules") or [],
            "character_rules": source.get("characterRules") or [],
            "language_rules": source.get("languageRules") or [],
            "rhythm_rules": source.get("rhythmRules") or [],
            "anti_ai_rules": source.get("antiAiRules") or [],
            "metrics": source.get("metrics") or {},
            "dimensions": source.get("dimensions") or {},
            "strengths": source.get("strengths") or [],
            "weaknesses": source.get("weaknesses") or [],
            "transition_plan": source.get("transitionPlan") or {},
            "report": source.get("report") or "",
            "sample_count": source.get("sampleCount") or 0,
            "sample_chars": source.get("sampleChars") or 0,
            "llm_used": source.get("llmUsed", False),
            "today": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


def delete_profiles_by_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM style_profiles WHERE project_id = ?", (project_id,))


# ══════════════════════════════════════════════════════
# style_bindings
# ══════════════════════════════════════════════════════

def get_binding(project_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM style_bindings WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "profileId": row["profile_id"],
        "scope": row["scope"],
        "priority": row["priority"],
        "weight": row["weight"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def upsert_binding(project_id: str, profile_id: str, today: str) -> None:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM style_bindings WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE style_bindings SET profile_id = ?, updated_at = ? WHERE project_id = ?",
                (profile_id, today, project_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO style_bindings (
                    id, project_id, profile_id, scope, priority, weight, created_at, updated_at
                ) VALUES (?, ?, ?, 'project', 1, 1.0, ?, ?)
                """,
                (gen_id(), project_id, profile_id, today, today),
            )


def delete_bindings_by_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM style_bindings WHERE project_id = ?", (project_id,))


# ══════════════════════════════════════════════════════
# style_transitions
# ══════════════════════════════════════════════════════

def get_transition(project_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM style_transitions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return row_to_transition(row) if row else None


def upsert_transition(project_id: str, fields: dict[str, Any], now: str) -> dict[str, Any]:
    """插入或更新过渡记录（project_id 唯一）。fields 键为 snake_case，JSON 已序列化。"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, started_at FROM style_transitions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE style_transitions
                SET profile_id = ?, pace = ?, progress = ?, interval_chapters = ?,
                    total_steps = ?, plan = ?, updated_at = ?,
                    started_at = CASE WHEN ? THEN ? ELSE started_at END
                WHERE project_id = ?
                """,
                (
                    fields.get("profile_id", ""),
                    fields.get("pace", "medium"),
                    fields.get("progress", 0),
                    fields.get("interval_chapters", 6),
                    fields.get("total_steps", 10),
                    json_dump(fields.get("plan", {})),
                    now,
                    int(bool(fields.get("reset_started_at"))),
                    now,
                    project_id,
                ),
            )
            transition_id = existing["id"]
            started_at = existing["started_at"]
        else:
            transition_id = gen_id()
            started_at = now
            conn.execute(
                """
                INSERT INTO style_transitions (
                    id, project_id, profile_id, pace, progress, interval_chapters,
                    total_steps, plan, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    project_id,
                    fields.get("profile_id", ""),
                    fields.get("pace", "medium"),
                    fields.get("progress", 0),
                    fields.get("interval_chapters", 6),
                    fields.get("total_steps", 10),
                    json_dump(fields.get("plan", {})),
                    started_at,
                    now,
                ),
            )
    return get_transition(project_id) or {}


def delete_transitions_by_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM style_transitions WHERE project_id = ?", (project_id,))
