"""chapter_fts FTS5 检索 DAO（M1 增强：关键词检索，验收 M7）。

背景：
- 主存储仍是 SQLite（PRD 约束：不引入向量库/图数据库/重型框架）。
- chapter_fts 是 FTS5 虚拟表，内容 = 章节标题 + 正文 + 摘要 + 事实内容；
  trigram 分词（SQLite ≥ 3.34）支持中文 3 字以上子串匹配，零外部依赖（不引入 jieba）。
- 检索结果含章节/事实的 source 追溯（chapter_id + chapter_order），支撑验收 M5。

同步钩子（幂等）：
- sync_chapter_fts：章节写入/更新/摘要变更/事实抽取后调用（先删后插）。
- 调用方在自身事务连接上传入 conn（避免测试/生产连接不一致）；
  未传入时使用本模块 get_conn（供维护脚本直接调用）。

降级策略：
- FTS5 不可用 / 表不存在：检索降级为 LIKE 兜底，绝不抛异常阻塞生成任务。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.dao.constants import (
    TABLE_CHAPTER_FTS,
    TABLE_CHAPTERS,
    TABLE_NOVEL_FACTS,
)
from app.db import get_conn
from app.utils.text_repair import repair_text

# 检索上限（注入预算内的 top-K）
DEFAULT_SEARCH_LIMIT = 5
FACT_SEARCH_LIMIT = 10
# 检索片段展示长度
SNIPPET_MAX = 160


def _utc_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _table_exists(conn: sqlite3.Connection) -> bool:
    """当前连接所在数据库是否存在 chapter_fts 虚拟表。"""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (TABLE_CHAPTER_FTS,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def sync_chapter_fts(
    project_id: str, chapter_id: str, conn: sqlite3.Connection | None = None
) -> bool:
    """同步单章 FTS 索引（幂等：先删后插）。

    内容聚合：章节标题 + 正文 + 摘要 + 该章事实内容。事实随 extract_facts 新增后，
    再次调用本函数即可让新事实进入索引（无需单独维护事实表）。
    返回是否真正写入（False = FTS 表不存在 / 章节不存在，静默跳过）。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _table_exists(conn):
            return False
        row = conn.execute(
            f"""
            SELECT title, content, summary FROM {TABLE_CHAPTERS}
            WHERE id = ? AND project_id = ?
            """,
            (chapter_id, project_id),
        ).fetchone()
        if row is None:
            return False
        fact_columns = {item["name"] for item in conn.execute(f"PRAGMA table_info({TABLE_NOVEL_FACTS})").fetchall()}
        active_clause = "AND status NOT IN ('superseded', 'conflict')" if "status" in fact_columns else ""
        facts_rows = conn.execute(
            f"""
            SELECT content FROM {TABLE_NOVEL_FACTS}
            WHERE project_id = ? AND chapter_id = ? AND is_conflict = 0
              {active_clause}
            ORDER BY created_at ASC, id ASC
            """,
            (project_id, chapter_id),
        ).fetchall()
        facts_text = "。".join(
            repair_text(r["content"]) for r in facts_rows if r["content"]
        )

        # 先删后插：幂等（重复同步不产生重复行）
        conn.execute(
            f"DELETE FROM {TABLE_CHAPTER_FTS} WHERE project_id = ? AND chapter_id = ?",
            (project_id, chapter_id),
        )
        conn.execute(
            f"""
            INSERT INTO {TABLE_CHAPTER_FTS}
                (project_id, chapter_id, title, content, summary, facts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                chapter_id,
                repair_text(row["title"] or ""),
                repair_text(row["content"] or ""),
                repair_text(row["summary"] or ""),
                facts_text,
            ),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚，索引实际未落库
        if own_conn:
            conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        if own_conn:
            conn.close()


def delete_chapter_fts(
    project_id: str, chapter_id: str, conn: sqlite3.Connection | None = None
) -> bool:
    """删除单章 FTS 索引（章节删除时调用，幂等）。"""
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _table_exists(conn):
            return False
        conn.execute(
            f"DELETE FROM {TABLE_CHAPTER_FTS} WHERE project_id = ? AND chapter_id = ?",
            (project_id, chapter_id),
        )
        # own_conn 模式下必须提交：否则 close() 时事务回滚
        if own_conn:
            conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        if own_conn:
            conn.close()


def _split_keywords(keywords: list[str] | None) -> list[str]:
    """去重、去空、去重词，保留 1-2 字短词供 LIKE 兜底、3 字以上供 FTS。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in keywords or []:
        kw = (raw or "").strip()
        if not kw:
            continue
        if kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def _fts_query(keywords: list[str]) -> str | None:
    """把 3 字以上关键词拼成 FTS5 MATCH 查询（引号包裹短语，OR 连接）。"""
    parts = [f'"{kw}"' for kw in keywords if len(kw) >= 3]
    if not parts:
        return None
    return " OR ".join(parts)


def _row_to_result(row: sqlite3.Row, *, source: str) -> dict[str, Any]:
    # FTS 分支：用 c.summary 或 fts_content 前缀生成 snippet；LIKE 分支：substr(content)
    snippet_src = row["snippet"] if "snippet" in row.keys() else (
        row["summary"] or row["fts_content"] or ""
    )
    return {
        "chapter_id": row["chapter_id"],
        "chapter_order": int(row["chapter_order"] or 0),
        "title": repair_text(row["title"] or ""),
        "snippet": repair_text(str(snippet_src))[:SNIPPET_MAX],
        "source": source,
    }


def search_relevant(
    project_id: str,
    keywords: list[str] | None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """按关键词检索相关章节（FTS5 优先，LIKE 兜底），返回按相关度排序的章节列表。

    结果含 chapter_id / chapter_order / title / snippet / source（来源追溯，验收 M5）。
    无关键词或表缺失时返回空列表，绝不抛异常（降级设计）。
    """
    kws = _split_keywords(keywords)
    if not kws:
        return []
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        fts_available = _table_exists(conn)
        query = _fts_query(kws)
        if fts_available and query:
            try:
                rows = conn.execute(
                    f"""
                    SELECT f.chapter_id, c.chapter_order, c.title, c.summary,
                           f.content AS fts_content
                    FROM {TABLE_CHAPTER_FTS} f
                    JOIN {TABLE_CHAPTERS} c
                      ON c.id = f.chapter_id AND c.project_id = f.project_id
                    WHERE f.project_id = ?
                      AND {TABLE_CHAPTER_FTS} MATCH ?
                    ORDER BY c.chapter_order ASC
                    LIMIT ?
                    """,
                    (project_id, query, max(1, limit)),
                ).fetchall()
                if rows:
                    return [_row_to_result(r, source="fts") for r in rows]
            except sqlite3.Error:
                # FTS 分支异常（MATCH/兼容性问题）静默降级到 LIKE 兜底，
                # 绝不让单点兼容问题导致整个检索失败（memory_fts 降级设计）
                pass

        # FTS 不可用或无命中：LIKE 兜底（对 1-2 字短词同样有效）
        like_clauses = []
        params: list[Any] = [project_id]
        for kw in kws:
            pattern = f"%{kw}%"
            like_clauses.append("(c.content LIKE ? OR c.title LIKE ? OR c.summary LIKE ?)")
            params.extend([pattern, pattern, pattern])
        rows = conn.execute(
            f"""
            SELECT c.id AS chapter_id, c.chapter_order, c.title,
                   substr(c.content, 1, {SNIPPET_MAX}) AS snippet
            FROM {TABLE_CHAPTERS} c
            WHERE c.project_id = ? AND ({' OR '.join(like_clauses)})
            ORDER BY c.chapter_order ASC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [_row_to_result(r, source="like") for r in rows]
    except sqlite3.Error:
        return []
    finally:
        if own_conn:
            conn.close()


def search_facts(
    project_id: str,
    keywords: list[str] | None,
    limit: int = FACT_SEARCH_LIMIT,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """按关键词检索事实账本（LIKE 匹配，FTS 表已含 facts 列但事实行级检索用本表）。

    返回事实行（含 chapter_id 来源，验收 M5 可追溯）；is_conflict=1 的写作冲突记录排除。
    """
    kws = _split_keywords(keywords)
    if not kws:
        return []
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        like_clauses = []
        params: list[Any] = [project_id]
        for kw in kws:
            like_clauses.append("content LIKE ?")
            params.append(f"%{kw}%")
        rows = conn.execute(
            f"""
            SELECT id, project_id, fact_type, content, chapter_id, created_at,
                   status, version, confidence
            FROM {TABLE_NOVEL_FACTS}
            WHERE project_id = ? AND is_conflict = 0
              AND ({' OR '.join(like_clauses)})
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        if own_conn:
            conn.close()


def rebuild_project_fts(
    project_id: str, conn: sqlite3.Connection | None = None
) -> int:
    """重建某项目全部章节的 FTS 索引（回填脚本 / 修复用，幂等）。

    返回成功同步的章节数。
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        if not _table_exists(conn):
            return 0
        rows = conn.execute(
            f"SELECT id FROM {TABLE_CHAPTERS} WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        synced = 0
        for row in rows:
            if sync_chapter_fts(project_id, row["id"], conn=conn):
                synced += 1
        # own_conn 模式下必须提交：否则 close() 时事务回滚
        if own_conn:
            conn.commit()
        return synced
    except sqlite3.Error:
        return 0
    finally:
        if own_conn:
            conn.close()
