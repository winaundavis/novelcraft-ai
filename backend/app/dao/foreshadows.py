"""foreshadows 表 DAO（Data Access Object）—— P2 伏笔账本（长程跨卷张力）。

伏笔（Foreshadow）是跨卷/跨几十章的长期承诺，管"待兑现"：状态机
planted（已埋设）→ progressing（推进中）→ resolved（已回收）/ abandoned（已放弃）。

与短程钩子（chapter_hooks，M3）职责分离：
- 钩子管"短程跨章张力"（每章至多一个，驱动读者读下一章）；
- 伏笔管"跨卷承诺/待兑现"（一次埋设，长程推进，最终回收/放弃）。

设计要点：
- plant_chapter_id / target_chapter_id 存大纲章节 id（跨章定位），章序号由
  service 层结合大纲解析（超期告警 / 展示需要序号）；
- events_json 存每次推进事件记录（[{chapter_id, chapter_order, status, note, resolution, at}]）；
- resolution 仅在 resolved/abandoned 时写入回收方式；
- 手动 PATCH 与自动追踪共用 update_foreshadow（后者带 event 自动追加事件）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.dao.constants import TABLE_FORESHADOWS
from app.db import get_conn
from app.models.schemas import gen_id

FORESHADOW_STATUS_PLANTED = "planted"
FORESHADOW_STATUS_PROGRESSING = "progressing"
FORESHADOW_STATUS_RESOLVED = "resolved"
FORESHADOW_STATUS_ABANDONED = "abandoned"
VALID_FORESHADOW_STATUSES = {
    FORESHADOW_STATUS_PLANTED,
    FORESHADOW_STATUS_PROGRESSING,
    FORESHADOW_STATUS_RESOLVED,
    FORESHADOW_STATUS_ABANDONED,
}
# 自动追踪允许的推进方向（不可回退到 planted；已埋设可直接放弃）
FORWARD_STATUSES = {
    FORESHADOW_STATUS_PLANTED: (
        FORESHADOW_STATUS_PROGRESSING,
        FORESHADOW_STATUS_RESOLVED,
        FORESHADOW_STATUS_ABANDONED,
    ),
    FORESHADOW_STATUS_PROGRESSING: (
        FORESHADOW_STATUS_RESOLVED,
        FORESHADOW_STATUS_ABANDONED,
    ),
}

MAX_NAME_LENGTH = 120        # 伏笔名称上限
MAX_DESCRIPTION_LENGTH = 500 # 伏笔描述（埋设方式/暗示内容/预期顿悟感）上限
MAX_RESOLUTION_LENGTH = 300  # 回收方式说明上限
MAX_EVENT_NOTE_LENGTH = 300  # 单条推进事件说明上限
MAX_EVENTS = 50              # 单条伏笔事件记录上限（防无限膨胀）
MAX_EVENTS_JSON_LENGTH = 6000  # events_json 序列化总长上限


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _json_load_events(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _json_dump_events(events: list[dict[str, Any]]) -> str:
    return json.dumps(events, ensure_ascii=False)


def _sanitize_name(value: str) -> str:
    return (value or "").strip()[:MAX_NAME_LENGTH]


def _sanitize_description(value: str) -> str:
    return (value or "").strip()[:MAX_DESCRIPTION_LENGTH]


def _sanitize_resolution(value: str) -> str:
    return (value or "").strip()[:MAX_RESOLUTION_LENGTH]


def _sanitize_status(value: str) -> str:
    if value not in VALID_FORESHADOW_STATUSES:
        return FORESHADOW_STATUS_PLANTED
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"] or "",
        "description": row["description"] or "",
        "plant_chapter_id": row["plant_chapter_id"] or "",
        "target_chapter_id": row["target_chapter_id"] or "",
        "status": row["status"] or FORESHADOW_STATUS_PLANTED,
        "resolution": row["resolution"] or "",
        "events": _json_load_events(row["events_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ── 写入 ──────────────────────────────────────────────

def create_foreshadow(
    project_id: str,
    *,
    name: str,
    description: str = "",
    plant_chapter_id: str = "",
    target_chapter_id: str = "",
    status: str = FORESHADOW_STATUS_PLANTED,
) -> str:
    """确认入库一条伏笔（status 默认 planted），返回新 id。"""
    name = _sanitize_name(name)
    if not name:
        name = "未命名伏笔"
    now = _utc_iso()
    fid = gen_id()
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TABLE_FORESHADOWS}
                (id, project_id, name, description, plant_chapter_id, target_chapter_id,
                 status, resolution, events_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', ?, ?)
            """,
            (
                fid,
                project_id,
                name,
                _sanitize_description(description),
                plant_chapter_id or "",
                target_chapter_id or "",
                _sanitize_status(status),
                now,
                now,
            ),
        )
    return fid


def create_foreshadows_batch(project_id: str, items: list[dict[str, Any]]) -> list[str]:
    """批量确认入库（用户勾选确认 → 落库，status=planted）。返回新 id 列表。"""
    return [
        create_foreshadow(
            project_id,
            name=str(item.get("name") or ""),
            description=str(item.get("description") or ""),
            plant_chapter_id=str(item.get("plant_chapter_id") or ""),
            target_chapter_id=str(item.get("target_chapter_id") or ""),
        )
        for item in items
    ]


# ── 查询 ──────────────────────────────────────────────

def get_foreshadow(project_id: str, fid: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM {TABLE_FORESHADOWS}
            WHERE id = ? AND project_id = ?
            """,
            (fid, project_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_foreshadows(project_id: str) -> list[dict[str, Any]]:
    """按创建顺序返回项目全部伏笔。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_FORESHADOWS}
            WHERE project_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (project_id,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_active_foreshadows(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """返回活跃伏笔（planted/progressing），供生成前注入与自动追踪。

    按时间倒序（最近的先返回），limit 控制注入体积（token 可控）。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM {TABLE_FORESHADOWS}
            WHERE project_id = ? AND status IN (?, ?)
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (project_id, FORESHADOW_STATUS_PLANTED, FORESHADOW_STATUS_PROGRESSING, limit),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_foreshadows(project_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {TABLE_FORESHADOWS} WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


def count_active_foreshadows(project_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM {TABLE_FORESHADOWS}
            WHERE project_id = ? AND status IN (?, ?)
            """,
            (project_id, FORESHADOW_STATUS_PLANTED, FORESHADOW_STATUS_PROGRESSING),
        ).fetchone()
    return int(row["n"]) if row else 0


# ── 更新 ──────────────────────────────────────────────

def update_foreshadow(
    project_id: str,
    fid: str,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    resolution: str | None = None,
    event_note: str = "",
    chapter_id: str = "",
    chapter_order: int = 0,
) -> bool:
    """更新一条伏笔；返回是否更新成功（不存在返回 False）。

    - 自动追踪（service 层）传入 status/resolution + event_note，自动追加事件记录；
    - 手动 PATCH（API 层）可只改 name/description/status/resolution 任一项；
      status 手动修改时同样记事件（便于追溯人工干预）。
    - status 变更按 FORWARD_STATUSES 校验，非法/回退状态拒绝（返回 False）。
    """
    existing = get_foreshadow(project_id, fid)
    if not existing:
        return False

    updates: dict[str, Any] = {}
    new_status = existing["status"]
    if status is not None:
        new_status = _sanitize_status(status)
        allowed = FORWARD_STATUSES.get(existing["status"], ())
        if new_status != existing["status"] and new_status not in allowed:
            return False
        updates["status"] = new_status
    if name is not None:
        updates["name"] = _sanitize_name(name) or "未命名伏笔"
    if description is not None:
        updates["description"] = _sanitize_description(description)
    if resolution is not None:
        updates["resolution"] = _sanitize_resolution(resolution)

    # 事件记录：状态变更或提供了事件说明时追加
    note = (event_note or "").strip()[:MAX_EVENT_NOTE_LENGTH]
    if updates.get("status") != existing["status"] or note:
        # get_foreshadow 返回的 events 已是解析后的列表（_row_to_dict 转换）
        events = list(existing.get("events") or [])
        events.append(
            {
                "chapter_id": chapter_id or "",
                "chapter_order": int(chapter_order or 0),
                "status": new_status,
                "note": note,
                "resolution": _sanitize_resolution(
                    updates.get("resolution", existing.get("resolution") or "")
                ),
                "at": _utc_iso(),
            }
        )
        events = events[-MAX_EVENTS:]
        updates["events_json"] = _json_dump_events(events)
        # 防止极端情况下事件累积导致行过大（兜底裁剪）
        if len(updates["events_json"]) > MAX_EVENTS_JSON_LENGTH:
            updates["events_json"] = _json_dump_events(events[-8:])

    if not updates:
        return True

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    params = [updates[column] for column in updates]
    params.append(_utc_iso())
    params.append(project_id)
    params.append(fid)
    with get_conn() as conn:
        cursor = conn.execute(
            f"""
            UPDATE {TABLE_FORESHADOWS}
            SET {set_clause}, updated_at = ?
            WHERE project_id = ? AND id = ?
            """,
            tuple(params),
        )
        return cursor.rowcount > 0


def delete_foreshadow(project_id: str, fid: str) -> bool:
    with get_conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM {TABLE_FORESHADOWS} WHERE project_id = ? AND id = ?",
            (project_id, fid),
        )
        return cursor.rowcount > 0


def delete_foreshadows_by_project(project_id: str) -> None:
    """删除某项目下全部伏笔（级联删除用）。"""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TABLE_FORESHADOWS} WHERE project_id = ?", (project_id,))
