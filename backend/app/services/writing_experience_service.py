"""Candidate-gated project writing experience memory."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from app.dao import writing_experiences as experience_dao

EXPERIENCE_RENDER_BUDGET = 1200
VALID_TYPES = {"successful_pattern", "recurring_problem", "prohibited_expression", "used_imagery", "resolved_conflict", "climax_pattern"}
_SENSITIVE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|AIza[\w-]{20,}|-----BEGIN .*PRIVATE KEY-----)")


def record(project_id: str, experience_type: str, observation: str, preferred_fix: str = "", source_chapters: list[str] | None = None) -> dict[str, Any]:
    observation = str(observation or "").strip()[:500]
    if experience_type not in VALID_TYPES:
        raise ValueError("不支持的写作经验类型。")
    if not observation or _SENSITIVE.search(observation) or _SENSITIVE.search(preferred_fix or ""):
        raise ValueError("写作经验为空或包含敏感信息。")
    return experience_dao.record_observation(project_id, experience_type, observation, str(preferred_fix or "").strip()[:500], [str(item) for item in (source_chapters or []) if str(item).strip()][:30])


def accept(project_id: str, experience_id: str) -> dict[str, Any]:
    result = experience_dao.accept(experience_id, project_id)
    if not result:
        raise ValueError("写作经验未找到。")
    return result


def render_accepted(project_id: str) -> str:
    try:
        rows = experience_dao.list_accepted(project_id)
    except sqlite3.OperationalError:
        return ""
    lines = ["【本项目已确认写作经验】"]
    for item in [row for row in rows if row.get("status", "accepted") == "accepted"][:8]:
        lines.append(f"- {item.get('experience_type')}: {item.get('observation')}；建议：{item.get('preferred_fix') or '保持当前处理'}")
    return "\n".join(lines)[:EXPERIENCE_RENDER_BUDGET] if len(lines) > 1 else ""
