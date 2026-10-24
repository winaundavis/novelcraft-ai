"""重写版本替换（验收 P1-1）回归测试。

覆盖三组事务测试 + 关键边界：
- 改设定：重写把"父亲叫沈建国"改成"沈怀舟" → 旧事实 superseded、新事实 active、
  注入查询（list_completed_revealed_facts）不再返回旧事实
- 删设定：重写删除某设定 → 该章旧事实 superseded
- 抽取失败：LLM 不可用 → 旧版本全部保留（不清空记忆）
- 改回原设定：superseded 同名事实"复活"（UNIQUE 约束下不丢版本）
- 实体/关系版本替换：旧实体 superseded、新实体 active
- 未改变设定：重写后旧事实保持 active（keep_contents 生效）
"""

from __future__ import annotations

import sqlite3
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.dao import memory_entities as me_dao
from app.dao import novel_facts as nf_dao
from app.llm.provider import LLMResponseError
from app.services import memory_service


@pytest.fixture
def full_memory_db(monkeypatch):
    """独立共享内存库（novel_facts + memory_entities + memory_relations）。

    用 SQLite shared-cache 内存库（file::memory:?cache=shared）：每个 DAO 调用
    get_conn() 拿到独立连接、可安全 close，但共享同一份数据（避免 own_conn
    模式 close 掉共享连接导致后续调用失败）。
    """
    uri = f"file:test_memory_rewrite_replace_{uuid.uuid4().hex}?mode=memory&cache=shared"
    init_conn = sqlite3.connect(uri, uri=True)
    init_conn.row_factory = sqlite3.Row
    init_conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS novel_facts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            fact_type TEXT NOT NULL DEFAULT 'completed',
            content TEXT NOT NULL,
            chapter_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            is_conflict INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            entities_json TEXT NOT NULL DEFAULT '[]',
            relations_json TEXT NOT NULL DEFAULT '[]',
            source_chapter_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(project_id, content)
        );
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
    init_conn.commit()

    def _get_conn():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    with (
        patch.object(nf_dao, "get_conn", _get_conn),
        patch.object(me_dao, "get_conn", _get_conn),
    ):
        # 注意：init_conn 保持打开（作为锚定连接），否则 shared-cache 内存库
        # 在最后一个连接关闭时被销毁，后续 get_conn 会看到空库。
        yield _get_conn()
    init_conn.close()


def _seed_old_version(full_memory_db):
    """写入"重写前"的旧版本：事实 + 实体 + 关系（source_chapter_id=c1）。"""
    nf_dao.add_fact(
        project_id="p1",
        fact_type="revealed",
        content="父亲叫沈建国",
        chapter_id="c1",
        source_chapter_id="c1",
    )
    nf_dao.add_fact(
        project_id="p1",
        fact_type="state_changed",
        content="沈建国离开老宅",
        chapter_id="c1",
        source_chapter_id="c1",
    )
    shen_id = me_dao.upsert_entity(
        project_id="p1",
        entity_type="person",
        name="沈建国",
        description="主角父亲",
        confidence=0.9,
        source_chapter_id="c1",
    )
    son_id = me_dao.upsert_entity(
        project_id="p1",
        entity_type="person",
        name="沈小安",
        description="主角",
        confidence=0.9,
        source_chapter_id="c1",
    )
    me_dao.upsert_relation(
        project_id="p1",
        entity_id=shen_id,
        relation_type="kinship",
        target_entity_id=son_id,
        confidence=0.9,
        source_chapter_id="c1",
    )
    return {"shen_id": shen_id, "son_id": son_id}


# ══════════════════════════════════════════════════════
# 场景1：改设定（沈建国 → 沈怀舟）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rewrite_change_setting_supersedes_old_facts(full_memory_db):
    """重写把"父亲叫沈建国"改成"沈怀舟"：旧事实 superseded，新事实 active 且可注入。"""
    _seed_old_version(full_memory_db)

    # 新版本抽取：只返回"沈怀舟"相关事实/实体
    async def fake_chat(messages, **kwargs):
        role_system = messages[0]["content"]
        if "不可逆事实" in role_system:
            return (
                '[{"fact_type": "revealed", "content": "父亲叫沈怀舟"}, '
                '{"fact_type": "state_changed", "content": "沈怀舟离开老宅"}]'
            )
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"description": "主角父亲", "confidence": 0.9}, '
            '{"entity_type": "person", "name": "沈小安", "description": "主角", '
            '"confidence": 0.9}], '
            '"relations": [{"from": "沈怀舟", "relation": "kinship", "to": "沈小安", "confidence": 0.9}]}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await memory_service.replace_chapter_memory("p1", "c1", "重写后的正文……")

    assert result["ok"] is True
    assert result["facts_written"] == 2

    # 旧事实"父亲叫沈建国"必须 superseded
    all_facts = nf_dao.list_facts("p1")
    old = next(f for f in all_facts if "沈建国" in f["content"] and "父亲" in f["content"])
    assert old["status"] == "superseded"
    # 新事实 active
    new = next(f for f in all_facts if "沈怀舟" in f["content"] and "父亲" in f["content"])
    assert new["status"] == "active"

    # 注入查询不得再返回旧设定
    injectable = nf_dao.list_completed_revealed_facts("p1")
    injectable_texts = [f["content"] for f in injectable]
    assert not any("父亲叫沈建国" in t for t in injectable_texts)
    assert any("父亲叫沈怀舟" in t for t in injectable_texts)

    # 实体：旧"沈建国" superseded，新"沈怀舟" active
    entities = me_dao.list_entities("p1", limit=100, include_superseded=True)
    old_ent = next(e for e in entities if e["name"] == "沈建国")
    new_ent = next(e for e in entities if e["name"] == "沈怀舟")
    assert old_ent["status"] == "superseded"
    assert new_ent["status"] == "active"


# ══════════════════════════════════════════════════════
# 场景2：删设定（重写后某事实不再出现）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rewrite_delete_setting_supersedes_old_fact(full_memory_db):
    """重写删除了"沈建国离开老宅"：该旧事实 superseded；仍存在的事实保持 active。"""
    _seed_old_version(full_memory_db)

    # 新版本只保留"父亲叫沈怀舟"，不再有"离开老宅"事件
    async def fake_chat(messages, **kwargs):
        role_system = messages[0]["content"]
        if "不可逆事实" in role_system:
            return '[{"fact_type": "revealed", "content": "父亲叫沈怀舟"}]'
        return (
            '{"entities": [{"entity_type": "person", "name": "沈怀舟", '
            '"description": "主角父亲", "confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=fake_chat)):
        result = await memory_service.replace_chapter_memory("p1", "c1", "重写后的正文……")

    all_facts = nf_dao.list_facts("p1")
    deleted = next(f for f in all_facts if "离开老宅" in f["content"])
    kept = next(f for f in all_facts if "父亲" in f["content"] and "沈怀舟" in f["content"])
    assert deleted["status"] == "superseded"
    assert kept["status"] == "active"


# ══════════════════════════════════════════════════════
# 场景3：抽取失败 → 旧版本全部保留
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rewrite_extract_failure_keeps_old_version(full_memory_db):
    """LLM 抽取失败（LLMResponseError）：不 supersede 任何旧数据，记忆不清空。"""
    _seed_old_version(full_memory_db)

    with patch.object(
        memory_service.llm, "chat", new=AsyncMock(side_effect=LLMResponseError("调用失败"))
    ):
        result = await memory_service.replace_chapter_memory("p1", "c1", "重写后的正文……")

    assert result["ok"] is False
    assert result["facts_written"] == 0
    # 旧版本全部保留 active
    all_facts = nf_dao.list_facts("p1")
    assert all(f["status"] == "active" for f in all_facts)
    entities = me_dao.list_entities("p1", limit=100, include_superseded=True)
    assert all(e["status"] == "active" for e in entities)


# ══════════════════════════════════════════════════════
# 场景4：改回原设定 → superseded 同名事实"复活"
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rewrite_revert_setting_reactivates_fact(full_memory_db):
    """先改成沈怀舟（旧沈建国 superseded），再改回沈建国：同 content 必须复活而非被 UNIQUE 忽略。"""
    _seed_old_version(full_memory_db)

    # 第一轮：改成沈怀舟
    async def change_chat(messages, **kwargs):
        if "不可逆事实" in messages[0]["content"]:
            return '[{"fact_type": "revealed", "content": "父亲叫沈怀舟"}]'
        return '{"entities": [{"entity_type": "person", "name": "沈怀舟", "confidence": 0.9}], "relations": []}'

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=change_chat)):
        await memory_service.replace_chapter_memory("p1", "c1", "第一版重写……")

    old = next(f for f in nf_dao.list_facts("p1") if "沈建国" in f["content"] and "父亲" in f["content"])
    assert old["status"] == "superseded"

    # 第二轮：改回沈建国（同 content 再次出现）
    async def revert_chat(messages, **kwargs):
        if "不可逆事实" in messages[0]["content"]:
            return '[{"fact_type": "revealed", "content": "父亲叫沈建国"}]'
        return '{"entities": [{"entity_type": "person", "name": "沈建国", "confidence": 0.9}], "relations": []}'

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=revert_chat)):
        await memory_service.replace_chapter_memory("p1", "c1", "第二版重写……")

    revived = next(f for f in nf_dao.list_facts("p1") if "沈建国" in f["content"] and "父亲" in f["content"])
    assert revived["status"] == "active"
    injectable = nf_dao.list_completed_revealed_facts("p1")
    assert any("父亲叫沈建国" in f["content"] for f in injectable)


# ══════════════════════════════════════════════════════
# 场景5：未改变设定 → 旧事实保持 active（keep_contents 生效）
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rewrite_unchanged_setting_keeps_old_facts(full_memory_db):
    """重写仅润色未改设定：新旧抽取 content 相同 → 旧事实不被 supersede。"""
    _seed_old_version(full_memory_db)

    async def same_chat(messages, **kwargs):
        if "不可逆事实" in messages[0]["content"]:
            return (
                '[{"fact_type": "revealed", "content": "父亲叫沈建国"}, '
                '{"fact_type": "state_changed", "content": "沈建国离开老宅"}]'
            )
        return (
            '{"entities": [{"entity_type": "person", "name": "沈建国", '
            '"description": "主角父亲", "confidence": 0.9}, '
            '{"entity_type": "person", "name": "沈小安", "description": "主角", '
            '"confidence": 0.9}], "relations": []}'
        )

    with patch.object(memory_service.llm, "chat", new=AsyncMock(side_effect=same_chat)):
        result = await memory_service.replace_chapter_memory("p1", "c1", "润色后的正文……")

    assert result["facts_superseded"] == 0
    all_facts = nf_dao.list_facts("p1")
    assert all(f["status"] == "active" for f in all_facts)
    entities = me_dao.list_entities("p1", limit=100, include_superseded=True)
    assert all(e["status"] == "active" for e in entities)
