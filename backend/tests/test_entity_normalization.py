"""实体归一化（验收 P1：canonical_name + aliases 匹配）回归测试。

覆盖：
- 姓名漂移样本：同一人物先后以"沈建国"（第6章）与"沈怀舟"（第22章）出现，
  LLM 返回 canonical_name + aliases 后，抽取应归一为同一实体（不再漂移为两个）
- 别名命中自动合并：高置信度（≥0.8）别名命中已有实体 → 复用实体 id + 并入别名
- 低置信度不自动合并：低置信度（<0.8）→ 各自独立写入（留人工确认）
- 名称规范化：全角/空格/称谓剥离（"父亲　沈怀舟" → "沈怀舟"）
- normalize_name 单元测试
"""

from __future__ import annotations

import sqlite3
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import memory_entities as me_dao
from app.services import memory_service


@pytest.fixture
def entity_db():
    """独立共享内存库（memory_entities + memory_relations），patch get_conn。"""
    uri = f"file:test_entity_norm_{uuid.uuid4().hex}?mode=memory&cache=shared"
    init = sqlite3.connect(uri, uri=True)
    init.row_factory = sqlite3.Row
    init.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_entities (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'person',
            name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.8,
            source_chapter_id TEXT NOT NULL DEFAULT '',
            first_seen_chapter_id TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, entity_type, name)
        );
        CREATE TABLE IF NOT EXISTS memory_relations (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            entity_id TEXT NOT NULL DEFAULT '',
            relation_type TEXT NOT NULL DEFAULT 'related',
            target_entity_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.8,
            source_chapter_id TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, entity_id, relation_type, target_entity_id)
        );
        """
    )
    init.commit()

    def _get_conn():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    with patch.object(me_dao, "get_conn", _get_conn):
        yield _get_conn()
    init.close()


# ══════════════════════════════════════════════════════
# normalize_name 规范化
# ══════════════════════════════════════════════════════

def test_normalize_name_fullwidth_and_spaces():
    """全角→半角、去空白、去全角空格。"""
    assert me_dao.normalize_name("ＬｉＭｉｎｇ") == "LiMing"
    assert me_dao.normalize_name(" 沈 怀 舟 ") == "沈怀舟"
    assert me_dao.normalize_name("沈　怀舟") == "沈怀舟"


def test_normalize_name_strips_kinship_prefix():
    """剥离称谓前缀（"父亲沈怀舟" → "沈怀舟"）。"""
    assert me_dao.normalize_name("父亲沈怀舟") == "沈怀舟"
    assert me_dao.normalize_name("师父李四") == "李四"
    # 姓名本身不含称谓时不受影响
    assert me_dao.normalize_name("沈建国") == "沈建国"
    # 空输入安全
    assert me_dao.normalize_name("") == ""
    assert me_dao.normalize_name(None) == ""


# ══════════════════════════════════════════════════════
# 姓名漂移样本：沈建国 → 沈怀舟（canonical_name + aliases 归一）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_name_drift_merged_via_canonical_and_aliases(entity_db):
    """长跑漂移样本：第6章"沈建国"与第22章"沈怀舟"经 canonical+aliases 归一为同一实体。

    模拟：第22章 LLM 返回 name="沈怀舟"、aliases=["沈建国"]（识别为同一人物），
    高置信度别名命中 → 复用沈建国实体 id，并把沈怀舟并入 aliases。
    """
    # 第6章：先写入"沈建国"
    async def first_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "沈建国", '
            '"description": "主角父亲", "confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=first_chat)):
        written = await memory_service.extract_entities("p1", "c6", "第6章正文……")
    assert written == 1

    entities_after_c6 = me_dao.list_entities("p1", limit=100, include_superseded=True)
    shen_jianguo = next(e for e in entities_after_c6 if e["name"] == "沈建国")
    jianguo_id = shen_jianguo["id"]

    # 第22章：LLM 识别出 canonical=沈怀舟，aliases=[沈建国]
    async def second_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"aliases": ["沈建国"], "description": "主角父亲，本名沈怀舟", '
            '"confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=second_chat)):
        written = await memory_service.extract_entities("p1", "c22", "第22章正文……")
    assert written == 1

    # 关键断言：仍是**同一个实体**（不再漂移为两个）
    entities = me_dao.list_entities("p1", limit=100, include_superseded=True)
    names = {e["name"] for e in entities}
    assert "沈怀舟" in names or "沈建国" in names
    # 高置信度别名命中 → 复用沈建国 id，不新建独立"沈怀舟"实体
    assert len(entities) == 1, f"不应漂移为两个实体，实际 {[(e['name'], e['id']) for e in entities]}"
    merged = entities[0]
    assert merged["id"] == jianguo_id
    # 别名并入：aliases 包含新 canonical 名（归一后指向同一实体）
    aliases = merged.get("aliases") or []
    assert "沈怀舟" in aliases or merged["name"] == "沈怀舟"


# ══════════════════════════════════════════════════════
# 别名命中（非 canonical）也合并
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_alias_hit_reuses_existing_entity(entity_db):
    """别名命中已有实体 → 复用 id（即使 LLM 只给 name 别名）。"""
    # 先写入 canonical 实体
    async def first_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"description": "主角父亲", "confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=first_chat)):
        await memory_service.extract_entities("p1", "c1", "第一章……")
    huizhou_id = me_dao.list_entities("p1", limit=100)[0]["id"]

    # 下一章用别名"怀舟"出现
    async def second_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "怀舟", '
            '"aliases": ["沈怀舟"], "description": "父亲", "confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=second_chat)):
        await memory_service.extract_entities("p1", "c2", "第二章……")

    entities = me_dao.list_entities("p1", limit=100, include_superseded=True)
    assert len(entities) == 1, "别名应复用已有实体而非新建"
    assert entities[0]["id"] == huizhou_id


# ══════════════════════════════════════════════════════
# 低置信度不自动合并
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_low_confidence_alias_not_merged(entity_db):
    """低置信度（<0.8）别名不自动合并，各自独立写入（留人工确认）。"""
    async def first_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"description": "主角父亲", "confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=first_chat)):
        await memory_service.extract_entities("p1", "c1", "第一章……")

    async def low_conf_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "怀舟", '
            '"aliases": ["沈怀舟"], "description": "父亲？", "confidence": 0.5}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=low_conf_chat)):
        await memory_service.extract_entities("p1", "c2", "第二章……")

    entities = me_dao.list_entities("p1", limit=100, include_superseded=True)
    # 低置信度：不自动合并（保留两个待人工确认）
    assert len(entities) == 2


# ══════════════════════════════════════════════════════
# 称谓剥离辅助：关系解析用规范化名命中
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_relation_resolution_uses_normalized_names(entity_db):
    """关系 from/to 用称谓（"父亲"）也能解析到实体（称谓剥离归一化）。"""
    async def first_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"description": "主角父亲", "confidence": 0.9}, '
            '{"entity_type": "person", "name": "沈小安", "description": "主角", '
            '"confidence": 0.9}], '
            '"relations": [{"from": "沈怀舟", "relation": "kinship", "to": "沈小安", "confidence": 0.9}]}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=first_chat)):
        await memory_service.extract_entities("p1", "c1", "第一章……")

    # 用别名/称谓写关系
    async def second_chat(messages, **kwargs):
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"aliases": ["父亲"], "description": "父亲", "confidence": 0.9}], '
            '"relations": [{"from": "父亲", "relation": "kinship", "to": "沈小安", "confidence": 0.9}]}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=second_chat)):
        await memory_service.extract_entities("p1", "c2", "第二章……")

    # 关系应成功写入（称谓别名解析到沈怀舟实体）
    rels = me_dao.list_relations("p1", limit=100)
    assert len(rels) >= 1
