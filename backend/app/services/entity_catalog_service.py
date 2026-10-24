"""Canonical character identity and deterministic text-evidence matching."""

from __future__ import annotations

import re
from typing import Any


def _canonical_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    slug = re.sub(r"[^\w]+", "_", raw, flags=re.UNICODE).strip("_")
    if not slug:
        raise ValueError("character identity requires an id or display name")
    return slug if slug.startswith("character_") else f"character_{slug}"


def _unique(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def build_character_catalog(characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize project characters without changing persisted project data."""
    catalog: list[dict[str, Any]] = []
    known: set[str] = set()
    for character in characters or []:
        display_name = str(character.get("display_name") or character.get("displayName") or character.get("name") or "").strip()
        canonical_id = _canonical_id(character.get("canonical_id") or character.get("canonicalId") or character.get("id") or display_name)
        if canonical_id in known:
            raise ValueError(f"duplicate canonical character id: {canonical_id}")
        aliases = _unique([display_name, *(character.get("aliases") or []), character.get("name")])
        if not display_name or not aliases:
            raise ValueError("character identity requires a display name")
        catalog.append({"id": canonical_id, "display_name": display_name, "aliases": aliases})
        known.add(canonical_id)
    return catalog


def match_character_mentions(text: str, catalog: list[dict[str, Any]]) -> list[str]:
    """Return IDs whose display name or alias occurs in text, in catalog order."""
    source = str(text or "")
    return [
        str(entity["id"])
        for entity in catalog or []
        if any(alias and alias in source for alias in entity.get("aliases") or [])
    ]


def resolve_character_ids(values: list[Any], catalog: list[dict[str, Any]]) -> list[str]:
    """Resolve legacy names, aliases, or IDs to catalog IDs without guessing."""
    aliases: dict[str, str] = {}
    for entity in catalog or []:
        canonical_id = str(entity["id"])
        aliases[canonical_id] = canonical_id
        for alias in entity.get("aliases") or []:
            aliases[str(alias)] = canonical_id
    result: list[str] = []
    for value in values or []:
        canonical_id = aliases.get(str(value).strip())
        if canonical_id and canonical_id not in result:
            result.append(canonical_id)
    return result


def match_scene_participants(text: str, catalog: list[dict[str, Any]]) -> list[str]:
    """Read explicit scene participation evidence; a name mention is insufficient."""
    labels = ("场景参与", "场景人物")
    values: list[str] = []
    for line in str(text or "").splitlines():
        normalized = line.strip()
        for label in labels:
            prefix = f"{label}："
            if normalized.startswith(prefix):
                values.extend(part.strip() for part in re.split(r"[，,；;]", normalized[len(prefix):]))
    result: list[str] = []
    for entity in catalog or []:
        aliases = entity.get("aliases") or []
        if any(value and value in aliases for value in values):
            result.append(str(entity["id"]))
    return result
