"""memory_entities / memory_relations 表 DAO（M1 增强：结构化实体记忆）。

- 实体账本：人物/地点/物品/阵营/世界规则/时间线/伏笔（entity_type），
  带状态机 active/superseded/conflict/uncertain、版本号、有效期、置信度、来源章节。
- 关系账本：实体间定向关系（亲缘/敌友/师承/拥有/位于…），同带状态/版本/有效期/置信度。
- 幂等：UNIQUE(project_id, entity_type, name) 与 UNIQUE(project_id, entity_id,
  relation_type, target_entity_id) —— 重复 upsert 不产生重复行，版本递增。
- 与 novel_facts 的 is_conflict 兼容：历史冲突事实 status 归为 conflict，但不影响注入。

支撑验收：M3（过期状态判定 valid_to）、M5（来源章节追溯 source_chapter_id）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app.dao.constants import (
    TABLE_MEMORY_ENTITIES,
    TABLE_MEMORY_RELATIONS,
)
from app.db import get_conn
from app.models.schemas import gen_id
from app.utils.text_repair import repair_text

# ── 实体类型 ──────────────────────────────────────────
ENTITY_TYPES = (
    "person",      # 人物
    "location",    # 地点
    "item",        # 物品
    "faction",     # 阵营/势力
    "world_rule",  # 世界规则
    "timeline",    # 时间线/纪年
    "foreshadow",  # 伏笔/悬而未决之事
)
# 状态机（与 novel_facts.status 同义，与 is_conflict 兼容）
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_CONFLICT = "conflict"
STATUS_UNCERTAIN = "uncertain"
VALID_STATUSES = {STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_CONFLICT, STATUS_UNCERTAIN}

# 关系类型（开放集合，仅作默认值与校验提示）
RELATION_TYPES = (
    "related", "kinship", "ally", "enemy", "master", "possesses",
    "located_at", "member_of", "created_by", "came_from", "knows", "loves",
)

# 单实体状态块注入上限
ENTITY_STATE_LIMIT = 8
ENTITY_STATE_MAX_CHARS = 400


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


# ── 名称规范化（验收 P1：实体归一化基础） ────────────────

# 常见称谓前缀（归一化时剥离；"父亲沈建国"→"沈建国"）
_KINSHIP_PREFIXES = (
    "父亲", "母亲", "爷爷", "奶奶", "外公", "外婆", "姥姥", "姥爷",
    "祖父", "祖母", "叔父", "伯父", "姑母", "姨母", "舅舅", "舅母",
    "哥哥", "姐姐", "弟弟", "妹妹", "大哥", "二哥", "三哥", "大姐",
    "师父", "师傅", "师尊", "恩师", "老爷", "少爷", "小姐", "夫人",
    "先生", "女士", "大人", "陛下", "殿下", "长老", "掌门", "宗主",
)

# 全角 → 半角映射（规范化统一字符）
_FULL_TO_HALF = {ord(c): ord(h) for c, h in zip(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)}


def normalize_name(name: str) -> str:
    """实体名规范化：全角→半角、去空白、剥离常见称谓前缀。

    用于别名匹配前的统一（验收 P1：规范化空格、称谓、全半角）。
    例："父亲　沈建国" → "沈建国"；"ＬｉＭｉｎｇ" → "LiMing"。
    """
    if not name:
        return ""
    text = name.translate(_FULL_TO_HALF).replace(" ", "").replace("\u3000", "")
    # 剥离称谓前缀（只剥一层，避免误伤姓名本身）
    for prefix in _KINSHIP_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    return text


def _alias_set(entity: dict[str, Any]) -> set[str]:
    """实体的规范化别名集合（含自身 name）。"""
    names = {normalize_name(entity.get("name") or "")}
    for alias in entity.get("aliases") or []:
        normalized = normalize_name(str(alias))
        if normalized:
            names.add(normalized)
    names.discard("")
    return names


# ── 实体 ──────────────────────────────────────────────

def upsert_entity(
    project_id: str,
    *,
    entity_type: str = "person",
    name: str,
    description: str = "",
    aliases: list[str] | None = None,
    status: str = STATUS_ACTIVE,
    confidence: float = 0.8,
    source_chapter_id: str = "",
    meta: dict[str, Any] | None = None,
    conn=None,
) -> str:
    """幂等写入实体（project_id + entity_type + name 唯一）。

    - 已存在：更新 description/aliases/confidence/source，status 仅在传入非默认值时覆盖；
      不改变 name 与 created_at，version 不变（实体身份稳定，变更走状态/版本字段）。
    - 不存在：新建，version=1，first_seen_chapter_id=source_chapter_id。
    返回实体 id。
    """
    entity_type = entity_type if entity_type in ENTITY_TYPES else "person"
    if status not in VALID_STATUSES:
        status = STATUS_ACTIVE
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        existing = conn.execute(
            f"""
            SELECT id, status FROM {TABLE_MEMORY_ENTITIES}
            WHERE project_id = ? AND entity_type = ? AND name = ?
            """,
            (project_id, entity_type, name),
        ).fetchone()
        now = _utc_iso()
        if existing:
            entity_id = existing["id"]
            conn.execute(
                f"""
                UPDATE {TABLE_MEMORY_ENTITIES}
                SET description = ?, aliases_json = ?, confidence = ?,
                    source_chapter_id = ?,
                    status = CASE WHEN ? = '' THEN status ELSE ? END,
                    meta_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    description or "",
                    json.dumps(aliases or [], ensure_ascii=False),
                    min(1.0, max(0.0, float(confidence))),
                    source_chapter_id or "",
                    status,
                    status,
                    json.dumps(meta or {}, ensure_ascii=False),
                    now,
                    entity_id,
                ),
            )
            # own_conn 模式下必须提交：否则 close() 时事务回滚（更新丢失）
            if own_conn:
                conn.commit()
            return entity_id
        entity_id = gen_id()
        conn.execute(
            f"""
            INSERT INTO {TABLE_MEMORY_ENTITIES}
                (id, project_id, entity_type, name, aliases_json, description,
                 status, confidence, source_chapter_id, first_seen_chapter_id,
                 version, valid_from, valid_to, meta_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, '', ?, ?, ?)
            """,
            (
                entity_id,
                project_id,
                entity_type,
                name,
                json.dumps(aliases or [], ensure_ascii=False),
                description or "",
                status,
                min(1.0, max(0.0, float(confidence))),
                source_chapter_id or "",
                source_chapter_id or "",
                now,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚（实体写不进去）
        if own_conn:
            conn.commit()
        return entity_id
    finally:
        if own_conn:
            conn.close()


def get_entity(project_id: str, entity_id: str, conn=None) -> dict[str, Any] | None:
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT * FROM {TABLE_MEMORY_ENTITIES} WHERE id = ? AND project_id = ?",
            (entity_id, project_id),
        ).fetchone()
        return _entity_row(row) if row else None
    finally:
        if own_conn:
            conn.close()


def _entity_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "entity_type": row["entity_type"],
        "name": repair_text(row["name"] or ""),
        "aliases": _json_load(row["aliases_json"], []),
        "description": repair_text(row["description"] or ""),
        "status": row["status"],
        "confidence": float(row["confidence"] or 0.8),
        "source_chapter_id": row["source_chapter_id"],
        "first_seen_chapter_id": row["first_seen_chapter_id"],
        "version": int(row["version"] or 1),
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "meta": _json_load(row["meta_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_entities(
    project_id: str,
    *,
    status: str | None = None,
    entity_type: str | None = None,
    limit: int = 100,
    conn=None,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """列出实体；可按 status / entity_type 过滤，默认只返回 active（防过期状态误用，验收 M3）。

    include_superseded=True 时连 superseded 一并返回（重写版本替换载入全量实体名→id 用）。
    """
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status is not None:
        where.append("status = ?")
        params.append(status)
    elif not include_superseded:
        where.append("status != ?")
        params.append(STATUS_SUPERSEDED)  # 默认排除已取代实体，避免过期状态误用
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_MEMORY_ENTITIES}
            WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [_entity_row(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def set_entity_status(
    project_id: str,
    entity_id: str,
    status: str,
    *,
    version: int | None = None,
    conn=None,
) -> bool:
    """更新实体状态（状态机流转：active→superseded/conflict/uncertain 等）。

    可选 version 递增（取代旧版本时调用方传 version+1）；返回是否更新成功。
    """
    if status not in VALID_STATUSES:
        return False
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        set_clause = "status = ?, updated_at = ?"
        params: list[Any] = [status, _utc_iso()]
        if version is not None:
            set_clause += ", version = ?"
            params.append(int(version))
        params.extend([entity_id, project_id])
        cur = conn.execute(
            f"UPDATE {TABLE_MEMORY_ENTITIES} SET {set_clause} WHERE id = ? AND project_id = ?",
            tuple(params),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚
        if own_conn:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


def supersede_entity(
    project_id: str,
    entity_id: str,
    *,
    valid_to: str = "",
    conn=None,
) -> bool:
    """把实体标记为已取代（superseded），并记录失效时间（支撑验收 M3 过期状态判定）。"""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        current = conn.execute(
            f"SELECT version FROM {TABLE_MEMORY_ENTITIES} WHERE id = ? AND project_id = ?",
            (entity_id, project_id),
        ).fetchone()
        if current is None:
            return False
        now = _utc_iso()
        cur = conn.execute(
            f"""
            UPDATE {TABLE_MEMORY_ENTITIES}
            SET status = ?, valid_to = ?, version = version + 1, updated_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (STATUS_SUPERSEDED, valid_to or now, now, entity_id, project_id),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚
        if own_conn:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


def supersede_entities_by_chapter(
    project_id: str,
    chapter_id: str,
    *,
    keep_names: set[str] | None = None,
    conn=None,
) -> int:
    """把某章来源的 active 实体中、不在 keep_names 内的标记为 superseded（重写版本替换）。

    用于重写后：新抽取实体写入（upsert 同 name 自动恢复 active）后，把
    "新版本不再包含"的旧实体失效。返回被标记的实体数。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT id, name FROM {TABLE_MEMORY_ENTITIES}
            WHERE project_id = ? AND source_chapter_id = ? AND status = ?
            """,
            (project_id, chapter_id, STATUS_ACTIVE),
        ).fetchall()
        now = _utc_iso()
        count = 0
        for row in rows:
            if keep_names is not None and row["name"] in keep_names:
                continue
            cur = conn.execute(
                f"""
                UPDATE {TABLE_MEMORY_ENTITIES}
                SET status = ?, valid_to = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_SUPERSEDED, now, now, row["id"]),
            )
            count += cur.rowcount
        if own_conn:
            conn.commit()
        return count
    finally:
        if own_conn:
            conn.close()


def supersede_relations_by_chapter(
    project_id: str,
    chapter_id: str,
    *,
    keep_keys: set[tuple[str, str, str]] | None = None,
    conn=None,
) -> int:
    """把某章来源的 active 关系中、不在 keep_keys 内的标记为 superseded（重写版本替换）。

    keep_keys 元素为 (entity_id, relation_type, target_entity_id) 三元组（新版本
    仍存在的关系）。返回被标记的关系数。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT id, entity_id, relation_type, target_entity_id
            FROM {TABLE_MEMORY_RELATIONS}
            WHERE project_id = ? AND source_chapter_id = ? AND status = ?
            """,
            (project_id, chapter_id, STATUS_ACTIVE),
        ).fetchall()
        now = _utc_iso()
        count = 0
        for row in rows:
            if keep_keys is not None and (
                row["entity_id"],
                row["relation_type"],
                row["target_entity_id"],
            ) in keep_keys:
                continue
            cur = conn.execute(
                f"""
                UPDATE {TABLE_MEMORY_RELATIONS}
                SET status = ?, valid_to = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_SUPERSEDED, now, now, row["id"]),
            )
            count += cur.rowcount
        if own_conn:
            conn.commit()
        return count
    finally:
        if own_conn:
            conn.close()


# ── 关系 ──────────────────────────────────────────────

def upsert_relation(
    project_id: str,
    *,
    entity_id: str,
    relation_type: str = "related",
    target_entity_id: str = "",
    status: str = STATUS_ACTIVE,
    confidence: float = 0.8,
    source_chapter_id: str = "",
    note: str = "",
    conn=None,
) -> str:
    """幂等写入关系（project_id + entity_id + relation_type + target_entity_id 唯一）。

    已存在：更新 status/confidence/note/source（status 非默认值时覆盖）；
    不存在：新建，version=1。返回关系 id。
    """
    if status not in VALID_STATUSES:
        status = STATUS_ACTIVE
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        existing = conn.execute(
            f"""
            SELECT id FROM {TABLE_MEMORY_RELATIONS}
            WHERE project_id = ? AND entity_id = ? AND relation_type = ? AND target_entity_id = ?
            """,
            (project_id, entity_id, relation_type, target_entity_id),
        ).fetchone()
        now = _utc_iso()
        if existing:
            conn.execute(
                f"""
                UPDATE {TABLE_MEMORY_RELATIONS}
                SET confidence = ?, source_chapter_id = ?, note = ?,
                    status = CASE WHEN ? = '' THEN status ELSE ? END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    min(1.0, max(0.0, float(confidence))),
                    source_chapter_id or "",
                    note or "",
                    status,
                    status,
                    now,
                    existing["id"],
                ),
            )
            # own_conn 模式下必须提交：否则 close() 时事务回滚（更新丢失）
            if own_conn:
                conn.commit()
            return existing["id"]
        rel_id = gen_id()
        conn.execute(
            f"""
            INSERT INTO {TABLE_MEMORY_RELATIONS}
                (id, project_id, entity_id, relation_type, target_entity_id,
                 status, confidence, source_chapter_id, version,
                 valid_from, valid_to, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, '', ?, ?, ?)
            """,
            (
                rel_id,
                project_id,
                entity_id,
                relation_type,
                target_entity_id,
                status,
                min(1.0, max(0.0, float(confidence))),
                source_chapter_id or "",
                now,
                note or "",
                now,
                now,
            ),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚（关系写不进去）
        if own_conn:
            conn.commit()
        return rel_id
    finally:
        if own_conn:
            conn.close()


def list_relations(
    project_id: str,
    *,
    entity_id: str | None = None,
    limit: int = 100,
    conn=None,
) -> list[dict[str, Any]]:
    """列出关系；默认排除 superseded；可按起点实体过滤。"""
    where = ["project_id = ?", "status != ?"]
    params: list[Any] = [project_id, STATUS_SUPERSEDED]
    if entity_id:
        where.append("entity_id = ?")
        params.append(entity_id)
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_MEMORY_RELATIONS}
            WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def delete_entities_by_project(project_id: str, conn=None) -> None:
    """删除某项目全部实体/关系（级联删除用）。"""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        conn.execute(
            f"DELETE FROM {TABLE_MEMORY_RELATIONS} WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            f"DELETE FROM {TABLE_MEMORY_ENTITIES} WHERE project_id = ?",
            (project_id,),
        )
    finally:
        if own_conn:
            conn.close()


def count_entities(project_id: str, conn=None) -> int:
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {TABLE_MEMORY_ENTITIES} WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        if own_conn:
            conn.close()


def find_entity_id(
    project_id: str, name: str, entity_type: str | None = None, conn=None
) -> str | None:
    """按名称精确查找实体 id（可选限定实体类型）；找不到返回 None。

    供实体抽取的"关系解析"使用：关系里的 from/to 实体名可能来自前几章，
    需要跨章节定位已入库实体。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        where = f"project_id = ? AND name = ?"
        params: list[Any] = [project_id, name]
        if entity_type:
            where += " AND entity_type = ?"
            params.append(entity_type)
        row = conn.execute(
            f"SELECT id FROM {TABLE_MEMORY_ENTITIES} WHERE {where} LIMIT 1",
            tuple(params),
        ).fetchone()
        return row["id"] if row else None
    finally:
        if own_conn:
            conn.close()


def find_entity_id_by_alias(
    project_id: str, name: str, *, entity_type: str | None = None, conn=None
) -> str | None:
    """按规范化名称 + 别名查找实体 id（验收 P1：实体归一化）。

    先精确 name 匹配（含规范化后匹配），再遍历 aliases_json 匹配；
    只匹配 active 实体（防过期状态误用）。返回 None 表示未找到。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        norm = normalize_name(name)
        if not norm:
            return None
        # 遍历 active 实体做规范化 name/别名匹配（项目内实体量有限，直接内存比对）
        rows = conn.execute(
            f"""
            SELECT id, name, aliases_json FROM {TABLE_MEMORY_ENTITIES}
            WHERE project_id = ? AND status = ?
            """,
            (project_id, STATUS_ACTIVE),
        ).fetchall()
        for r in rows:
            alias_names = {normalize_name(r["name"] or "")}
            for a in _json_load(r["aliases_json"], []):
                alias_names.add(normalize_name(str(a)))
            if norm in alias_names:
                return r["id"]
        return None
    finally:
        if own_conn:
            conn.close()


def merge_entities(
    project_id: str,
    keep_id: str,
    drop_id: str,
    *,
    conn=None,
) -> bool:
    """把 drop 实体合并进 keep（验收 P1：高置信度自动合并）。

    - drop 的关系全部转移给 keep（target/entity 两端）；
    - drop 标记 superseded；
    - keep 的 aliases 并入 drop 的 name + aliases（规范化去重）。
    返回是否成功（任一实体不存在返回 False）。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        keep = conn.execute(
            f"SELECT * FROM {TABLE_MEMORY_ENTITIES} WHERE id = ? AND project_id = ?",
            (keep_id, project_id),
        ).fetchone()
        drop = conn.execute(
            f"SELECT * FROM {TABLE_MEMORY_ENTITIES} WHERE id = ? AND project_id = ?",
            (drop_id, project_id),
        ).fetchone()
        if keep is None or drop is None or keep_id == drop_id:
            return False
        # 1) 关系转移（drop 作为起点 / 终点）
        conn.execute(
            f"""
            UPDATE {TABLE_MEMORY_RELATIONS}
            SET entity_id = ?, source_chapter_id = ?, updated_at = ?
            WHERE project_id = ? AND entity_id = ?
            """,
            (keep_id, drop["source_chapter_id"] or keep["source_chapter_id"], _utc_iso(),
             project_id, drop_id),
        )
        conn.execute(
            f"""
            UPDATE {TABLE_MEMORY_RELATIONS}
            SET target_entity_id = ?, updated_at = ?
            WHERE project_id = ? AND target_entity_id = ?
            """,
            (keep_id, _utc_iso(), project_id, drop_id),
        )
        # 2) keep aliases 并入 drop 的 name + aliases
        keep_aliases = set(_json_load(keep["aliases_json"], []))
        keep_aliases.add(drop["name"])
        for a in _json_load(drop["aliases_json"], []):
            keep_aliases.add(str(a))
        # 去重（规范化后比较），保留原样
        unique: list[str] = []
        seen: set[str] = set()
        for a in keep_aliases:
            norm = normalize_name(a)
            if norm and norm not in seen:
                seen.add(norm)
                unique.append(a)
        conn.execute(
            f"""
            UPDATE {TABLE_MEMORY_ENTITIES}
            SET aliases_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(unique, ensure_ascii=False), _utc_iso(), keep_id),
        )
        # 3) drop 标记 superseded
        now = _utc_iso()
        conn.execute(
            f"""
            UPDATE {TABLE_MEMORY_ENTITIES}
            SET status = ?, valid_to = ?, version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (STATUS_SUPERSEDED, now, now, drop_id),
        )
        if own_conn:
            conn.commit()
        return True
    finally:
        if own_conn:
            conn.close()


# ── 注入块：实体状态（验收 M1 分层上下文「最新状态」块） ──

def build_entity_state_block(
    project_id: str,
    limit: int = ENTITY_STATE_LIMIT,
    conn=None,
) -> str:
    """组装「实体状态」注入块：最近更新的活跃实体 + 相关关系。

    返回空串表示暂无实体（起始章节 / 表缺失）。超限截断到 ENTITY_STATE_MAX_CHARS。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        entities = list_entities(project_id, limit=limit, conn=conn)
        if not entities:
            return ""
        lines = ["【实体状态】以下为当前生效的人物/设定状态，请保持一致性："]
        for entity in entities:
            status_note = ""
            if entity["status"] == STATUS_UNCERTAIN:
                status_note = "（待核实）"
            elif entity["status"] == STATUS_CONFLICT:
                status_note = "（存在冲突）"
            name = entity["name"]
            if entity.get("aliases"):
                name = f"{name}（别名：{'、'.join(entity['aliases'][:3])}）"
            desc = entity.get("description") or ""
            if desc:
                lines.append(f"- {name}：{desc[:60]}{status_note}")
            else:
                lines.append(f"- {name}{status_note}")
        block = "\n".join(lines)
        return block[:ENTITY_STATE_MAX_CHARS]
    except Exception:  # noqa: BLE001  降级：任何异常返回空块，不阻塞生成
        return ""
    finally:
        if own_conn:
            conn.close()
