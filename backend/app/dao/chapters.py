"""chapters 表 DAO（Data Access Object）—— 封装所有对 chapters 表的 SQL 操作。"""

from __future__ import annotations

from typing import Any

from app.dao.constants import TABLE_CHAPTERS, TABLE_PROJECTS
from app.db import get_conn, row_to_chapter, utc_today
from app.utils.text_repair import repair_text


def list_chapters(project_id: str) -> list[dict[str, Any]]:
    """获取指定项目的全部章节，按 chapter_order + created_at 升序排列。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_CHAPTERS}
            WHERE project_id = ?
            ORDER BY chapter_order ASC, created_at ASC
            """,
            (project_id,),
        ).fetchall()
    return [row_to_chapter(row) for row in rows]


def list_chapters_global_order(project_id: str) -> list[dict[str, Any]]:
    """获取指定项目的全部章节，按**多卷全局顺序**排列（Codex P2 复测 P1）。

    问题：`chapter_order` 每卷从 1 重新开始，`list_chapters` 的
    `ORDER BY chapter_order` 会把「第一卷 第1章、第二卷 第1章……」交错排列，
    导致跨章重复检测等依赖章节顺序的逻辑拿到错误顺序。

    修复：LEFT JOIN volumes，先按卷的 `order`（卷顺序）排序，再按卷内
    `chapter_order` 排序；无卷归属的章节（volume_id 为空）排在最后。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT c.* FROM {TABLE_CHAPTERS} c
            LEFT JOIN volumes v ON v.project_id = c.project_id AND v.id = c.volume_id
            WHERE c.project_id = ?
            ORDER BY
                CASE WHEN c.volume_id = '' THEN 1 ELSE 0 END,
                v."order" ASC,
                c.chapter_order ASC,
                c.created_at ASC
            """,
            (project_id,),
        ).fetchall()
    return [row_to_chapter(row) for row in rows]


def get_chapter(chapter_id: str, project_id: str) -> dict[str, Any] | None:
    """按主键 + 项目 ID 查询单个章节。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_CHAPTERS} WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        ).fetchone()
    return row_to_chapter(row) if row else None


def get_chapter_content(project_id: str, chapter_id: str) -> str:
    """仅读取章节正文内容（纯文本），用于 AI 续写上下文。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT content FROM {TABLE_CHAPTERS} WHERE project_id = ? AND id = ?",
            (project_id, chapter_id),
        ).fetchone()
    from app.utils.text_repair import repair_text
    return repair_text(row["content"]) if row and row["content"] else ""


def insert_chapter(fields: dict[str, Any]) -> None:
    """插入一条章节记录。
    
    fields 字典的键为数据库列名（snake_case），值已序列化。
    """
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = tuple(fields[col] for col in columns)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {TABLE_CHAPTERS} ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


def upsert_chapter(chapter_id: str, project_id: str, fields: dict[str, Any], today: str) -> dict[str, Any]:
    """插入或更新章节（由调用方决定是 INSERT 还是 UPDATE）。
    
    返回更新后的章节字典。
    """
    # 先检查是否存在
    with get_conn() as conn:
        existing = conn.execute(
            f"SELECT * FROM {TABLE_CHAPTERS} WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        ).fetchone()

        if existing is None:
            # 新章节：INSERT
            insert_fields = {
                "id": chapter_id,
                "project_id": project_id,
                "volume_id": fields.get("volume_id", ""),
                "title": fields.get("title", "Untitled Chapter"),
                "chapter_order": fields.get("chapter_order", 1),
                "content": fields.get("content", ""),
                "word_count": fields.get("word_count", 0),
                "status": fields.get("status", "writing"),
                "summary": fields.get("summary", ""),
                "notes": fields.get("notes", ""),
                "created_at": today,
                "updated_at": today,
            }
            insert_chapter(insert_fields)
        else:
            # 已有章节：UPDATE
            update_fields = {}
            if "volume_id" in fields:
                update_fields["volume_id"] = fields["volume_id"]
            if "title" in fields:
                update_fields["title"] = fields["title"]
            if "chapter_order" in fields:
                update_fields["chapter_order"] = fields["chapter_order"]
            if "content" in fields:
                update_fields["content"] = fields["content"]
            if "word_count" in fields:
                update_fields["word_count"] = fields["word_count"]
            if "status" in fields:
                update_fields["status"] = fields["status"]
            if "summary" in fields:
                update_fields["summary"] = fields["summary"]
            update_fields["updated_at"] = today

            set_clauses = [f"{col} = ?" for col in update_fields]
            params = list(update_fields.values())
            params.extend([chapter_id, project_id])
            conn.execute(
                f"UPDATE {TABLE_CHAPTERS} SET {', '.join(set_clauses)} WHERE id = ? AND project_id = ?",
                tuple(params),
            )

        # 读取最新记录返回
        row = conn.execute(
            f"SELECT * FROM {TABLE_CHAPTERS} WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        ).fetchone()

    return row_to_chapter(row)


def delete_chapter(chapter_id: str, project_id: str) -> int:
    """删除章节记录，返回受影响行数（0 表示章节不存在）。"""
    with get_conn() as conn:
        result = conn.execute(
            f"DELETE FROM {TABLE_CHAPTERS} WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        )
        return result.rowcount


def delete_chapters_by_project(project_id: str) -> None:
    """删除某项目下的全部章节（级联删除用）。"""
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM {TABLE_CHAPTERS} WHERE project_id = ?", (project_id,)
        )


def aggregate_word_count(project_id: str) -> int:
    """聚合统计某项目的总字数。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(word_count), 0) AS total FROM {TABLE_CHAPTERS} WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["total"]) if row else 0


def list_completed_chapter_ids(project_id: str) -> set[str]:
    """M2 断点续传：返回某项目所有 status='completed' 的章节 id 集合。

    整书任务每章成功落库时写入 status='completed'，恢复任务据此跳过已生成章节，
    避免重启后重复生成（以数据库落库状态为准，不依赖内存）。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id FROM {TABLE_CHAPTERS}
            WHERE project_id = ? AND status = 'completed'
            """,
            (project_id,),
        ).fetchall()
    return {row["id"] for row in rows}


# ── M1 记忆底座：章节摘要相关 ──────────────────────────

def update_chapter_summary(project_id: str, chapter_id: str, summary: str) -> None:
    """更新单章摘要（每章生成后落库）。"""
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {TABLE_CHAPTERS} SET summary = ?, updated_at = ? WHERE id = ? AND project_id = ?",
            (summary, utc_today(), chapter_id, project_id),
        )


def list_recent_summaries(
    project_id: str,
    limit: int = 5,
    before_chapter_order: int | None = None,
    exclude_chapter_ids: set[str] | list[str] | None = None,
) -> list[str]:
    """读取最近 N 个已落库章节摘要（按 chapter_order 倒序，旧章节 summary 为空自动跳过）。

    供 M1 分层记忆注入："最近 5 章摘要"。

    P2-1：传入 `before_chapter_order`（当前章 order）时只返回当前章节**之前**的摘要，
    防止极端重写场景注入未来章节摘要（剧透）。

    BUG-B（记忆升级 v1）：新增 `exclude_chapter_ids`——大纲节点 `order` 为卷内序号
    （100 章仅 34 个唯一 order）时，`chapter_order < N` 过滤失效。调用方把"当前章
    之后的所有章节 id"传入，按 id 精确排除未来章节，彻底杜绝剧透。
    """
    where = f"project_id = ? AND summary != ''"
    params: list[Any] = [project_id]
    if before_chapter_order is not None:
        where += " AND chapter_order < ?"
        params.append(before_chapter_order)
    if exclude_chapter_ids:
        placeholders = ", ".join("?" for _ in exclude_chapter_ids)
        where += f" AND id NOT IN ({placeholders})"
        params.extend(exclude_chapter_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT summary FROM {TABLE_CHAPTERS}
            WHERE {where}
            ORDER BY chapter_order DESC, created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [repair_text(row["summary"]) for row in rows]


def list_recent_summaries_detailed(
    project_id: str,
    limit: int = 5,
    before_chapter_order: int | None = None,
    exclude_chapter_ids: set[str] | list[str] | None = None,
) -> list[dict[str, str]]:
    """同 `list_recent_summaries`，但返回带 `chapter_id` 的明细（新→旧）。

    供记忆召回日志（memory_recall_log）记录 source_ids（验收 M1/M8），
    与注入文本保持同一查询条件，保证日志与真实注入一致。
    """
    where = f"project_id = ? AND summary != ''"
    params: list[Any] = [project_id]
    if before_chapter_order is not None:
        where += " AND chapter_order < ?"
        params.append(before_chapter_order)
    if exclude_chapter_ids:
        placeholders = ", ".join("?" for _ in exclude_chapter_ids)
        where += f" AND id NOT IN ({placeholders})"
        params.extend(exclude_chapter_ids)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, summary FROM {TABLE_CHAPTERS}
            WHERE {where}
            ORDER BY chapter_order DESC, created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return [
        {"chapter_id": row["id"], "summary": repair_text(row["summary"])} for row in rows
    ]


def list_summaries_by_ids(project_id: str, chapter_ids: list[str]) -> list[dict[str, str]]:
    """按章节 ID 列表读取摘要（保持传入顺序），供卷摘要压缩使用。"""
    if not chapter_ids:
        return []
    placeholders = ", ".join("?" for _ in chapter_ids)
    params: list[Any] = [project_id, *chapter_ids]
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, summary FROM {TABLE_CHAPTERS}
            WHERE project_id = ? AND id IN ({placeholders})
            """,
            tuple(params),
        ).fetchall()
    by_id = {row["id"]: repair_text(row["summary"]) for row in rows}
    return [{"chapter_id": cid, "summary": by_id.get(cid, "")} for cid in chapter_ids]


def get_chapter_summary(project_id: str, chapter_id: str) -> str:
    """读取单章摘要；无记录 / 摘要为空返回空串。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT summary FROM {TABLE_CHAPTERS} WHERE id = ? AND project_id = ?",
            (chapter_id, project_id),
        ).fetchone()
    return repair_text(row["summary"]) if row and row["summary"] else ""


def get_previous_chapter(
    project_id: str,
    current_chapter_id: str,
    chapter_ids_in_order: list[str] | None = None,
) -> dict[str, Any] | None:
    """返回当前章的真正前一章（已落库章节记录）；无则 None。

    BUG-B（记忆升级 v1）：大纲节点 `order` 是**卷内序号**（跨卷重复，如多卷项目
    每卷都有 order=1），`chapters.chapter_order` 也直接沿用该值，因此按
    `chapter_order < N` 或大纲 order 定位前一章会跨卷错乱。

    正确做法：调用方传入 `chapter_ids_in_order`（按大纲全局顺序排列的章节 id 列表，
    由 novel_generation_service._chapter_nodes_from_outline 派生），本函数在全局序中
    取当前章的前一个 id 并回查 chapters 表（须已落库）。

    未传入时回退为 chapters 表 `(chapter_order, created_at)` 降序的保守定位
    （仅用于不掌握全局序的调用方；order 重复场景下不保证精确）。
    """
    if chapter_ids_in_order:
        for index, cid in enumerate(chapter_ids_in_order):
            if cid == current_chapter_id:
                if index > 0:
                    prev_id = chapter_ids_in_order[index - 1]
                    return get_chapter(prev_id, project_id)
                return None
    # 回退：按 chapter_order + created_at 降序，取比当前记录更早的最近一章
    current = get_chapter(current_chapter_id, project_id)
    if current is None:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_CHAPTERS}
            WHERE project_id = ? AND chapter_order < ?
            ORDER BY chapter_order DESC, created_at DESC
            LIMIT 1
            """,
            (project_id, int(current["chapterOrder"] or 0)),
        ).fetchall()
    if not rows:
        return None
    return dict(rows[0])


# ── 事务内辅助函数（接受外部 conn，用于多步操作在同一事务中执行） ──

def _insert_chapter_on_conn(conn, fields: dict[str, Any]) -> None:
    """在给定连接上插入章节（供外部事务块使用）。"""
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = tuple(fields[col] for col in columns)
    conn.execute(
        f"INSERT INTO {TABLE_CHAPTERS} ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def _update_chapter_on_conn(conn, chapter_id: str, project_id: str, fields: dict[str, Any]) -> None:
    """在给定连接上更新章节（供外部事务块使用）。"""
    set_clauses = [f"{col} = ?" for col in fields]
    params = list(fields.values())
    params.extend([chapter_id, project_id])
    conn.execute(
        f"UPDATE {TABLE_CHAPTERS} SET {', '.join(set_clauses)} WHERE id = ? AND project_id = ?",
        tuple(params),
    )
