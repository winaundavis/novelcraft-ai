"""Secret-free request manifest for a production-like Chapter 1 smoke dry run."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.dao import chapter_contracts_dao, chapters as chapters_dao, projects as projects_dao
from app.llm.resolver import get_runtime_provider
from app.llm.provider import llm
from app.llm.roles import ROLE_WRITING


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_manifest(project_id: str, chapter_id: str) -> dict[str, Any]:
    """Resolve the Chapter 1 request prerequisites without emitting prompt text or secrets."""
    project = projects_dao.get_project(project_id)
    if not project:
        raise ValueError("project not found")
    chapter = chapters_dao.get_chapter(chapter_id, project_id)
    if not chapter:
        raise ValueError("chapter not found")

    contract = chapter_contracts_dao.get_latest_contract(project_id, chapter_id) or {}
    style = project.get("styleConfig") or {}
    story = {
        "premise": project.get("premise") or "",
        "characters": project.get("characters") or [],
        "world_rules": project.get("worldRules") or [],
        "outline": project.get("outline") or {},
        "chapter": {"id": chapter_id, "title": chapter.get("title") or ""},
    }
    request = get_runtime_provider(llm).request_manifest(ROLE_WRITING)
    return {
        **request,
        "project_id": project_id,
        "chapter_id": chapter_id,
        "story_hash": _hash(story),
        "style_profile": {
            "name": str(style.get("name") or ""),
            "hash": _hash(style),
        },
        "chapter_contract": {
            "id": str(contract.get("id") or ""),
            "version": int(contract.get("version") or 0),
            "hash": _hash(contract),
        },
    }
