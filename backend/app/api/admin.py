"""运营后台 API（仅管理员）。

设计约束（刻意保持窄）：
* **零写操作**，除了「创建邀请码 / 停用邀请码」——避免误操作伤害真实用户。
* **不看作品正文**：只返回统计数字，不返回任何小说内容。
* 全部端点挂在 `require_maintainer` 后：普通用户一律 403。
* 统计口径以 `project_llm_usage`（项目+UTC 日）为主，`llm_audit_log` 只在需要明细时使用；
  两张表都可能还没有数据（懒建表），所以查询必须容忍表不存在。
"""

from __future__ import annotations

import json
import shutil
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_maintainer
from app.core.config import settings
from app.dao import invites as invites_dao
from app.db import get_conn
from app.models.schemas import ApiResponse

router = APIRouter(prefix="/admin", tags=["admin"])

_PROCESS_STARTED_AT = time.time()
_MAX_DAYS = 90


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _window_start(days: int) -> str:
    """返回「含今天在内最近 days 天」的起始日期（UTC，YYYY-MM-DD）。"""
    span = max(1, min(int(days or 1), _MAX_DAYS))
    return (datetime.now(timezone.utc) - timedelta(days=span - 1)).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_rows(sql: str, params: tuple = ()) -> list:
    """执行查询；表尚未创建（懒建表）时返回空列表而不是 500。"""
    try:
        with closing(get_conn()) as conn:
            return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 - 统计接口不应因缺表而整体失败
        return []


def _usage_by_owner(since: str) -> dict[str, dict[str, int]]:
    rows = _safe_rows(
        "SELECT p.owner_id AS owner_id, SUM(u.calls) AS calls, SUM(u.tokens) AS tokens "
        "FROM project_llm_usage u JOIN projects p ON p.id = u.project_id "
        "WHERE u.usage_date >= ? GROUP BY p.owner_id",
        (since,),
    )
    return {
        str(row["owner_id"]): {"calls": int(row["calls"] or 0), "tokens": int(row["tokens"] or 0)}
        for row in rows
        if row["owner_id"]
    }


def _backup_status() -> dict | None:
    """读取备份脚本写的状态文件；不存在时返回 None（不伪造数据）。"""
    path = Path(settings.data_dir).parent / "backup-status.json"
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _disk_status() -> dict | None:
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError:
        return None
    used_percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    return {
        "totalBytes": usage.total,
        "usedBytes": usage.used,
        "freeBytes": usage.free,
        "usedPercent": used_percent,
    }


def _count(table: str) -> int:
    rows = _safe_rows(f"SELECT COUNT(*) AS n FROM {table}")
    return int(rows[0]["n"]) if rows else 0


@router.get("/overview")
async def admin_overview(_: str = Depends(require_maintainer)) -> ApiResponse:
    """系统监控：就绪状态 + 运行信息 + 备份 + 磁盘 + 数据规模。"""
    try:
        from main import _readiness_state  # 延迟导入，避免 main ↔ api 的循环依赖

        ready, errors = _readiness_state()
    except Exception:  # noqa: BLE001 - 就绪探测失败本身也要能展示出来
        ready, errors = False, ["readiness_unavailable"]

    invite_counts = {}
    try:
        invite_counts = invites_dao.count_by_status()
    except Exception:  # noqa: BLE001
        invite_counts = {}

    return ApiResponse(
        data={
            "readiness": {"ready": bool(ready), "errors": list(errors)},
            "serverTimeUtc": _utc_now_iso(),
            "uptimeSeconds": int(time.time() - _PROCESS_STARTED_AT),
            "backup": _backup_status(),
            "disk": _disk_status(),
            "counts": {
                "users": _count("users"),
                "projects": _count("projects"),
                "chapters": _count("chapters"),
                "invitesPending": int(invite_counts.get("pending", 0)),
                "invitesBound": int(invite_counts.get("bound", 0)),
            },
        }
    )


@router.get("/users")
async def admin_users(days: int = 7, _: str = Depends(require_maintainer)) -> ApiResponse:
    """用户列表与用量。只返回数字，不返回任何作品内容。"""
    today = _today()
    since = _window_start(days)
    today_usage = _usage_by_owner(today)
    window_usage = _usage_by_owner(since)

    project_rows = _safe_rows("SELECT owner_id, COUNT(*) AS n FROM projects GROUP BY owner_id")
    projects_by_owner = {str(r["owner_id"]): int(r["n"]) for r in project_rows if r["owner_id"]}

    chapter_rows = _safe_rows(
        "SELECT p.owner_id AS owner_id, COUNT(c.id) AS n FROM chapters c "
        "JOIN projects p ON p.id = c.project_id GROUP BY p.owner_id"
    )
    chapters_by_owner = {str(r["owner_id"]): int(r["n"]) for r in chapter_rows if r["owner_id"]}

    admin_ids = {item.strip() for item in str(settings.admin_user_ids or "").split(",") if item.strip()}
    users = []
    for row in _safe_rows("SELECT id, username, is_admin, created_at FROM users ORDER BY created_at"):
        user_id = str(row["id"])
        day = today_usage.get(user_id, {"calls": 0, "tokens": 0})
        window = window_usage.get(user_id, {"calls": 0, "tokens": 0})
        users.append(
            {
                "id": user_id,
                "username": row["username"],
                "isAdmin": bool(row["is_admin"]) or user_id in admin_ids,
                "createdAt": row["created_at"],
                "projectCount": projects_by_owner.get(user_id, 0),
                "chapterCount": chapters_by_owner.get(user_id, 0),
                "todayCalls": day["calls"],
                "todayTokens": day["tokens"],
                "windowCalls": window["calls"],
                "windowTokens": window["tokens"],
            }
        )
    return ApiResponse(data={"days": max(1, min(int(days or 1), _MAX_DAYS)), "users": users})


@router.get("/usage")
async def admin_usage(days: int = 7, _: str = Depends(require_maintainer)) -> ApiResponse:
    """调用次数与 Token 用量：按日趋势 + 项目排行。"""
    since = _window_start(days)
    day_rows = _safe_rows(
        "SELECT usage_date AS d, SUM(calls) AS calls, SUM(tokens) AS tokens "
        "FROM project_llm_usage WHERE usage_date >= ? GROUP BY usage_date ORDER BY usage_date",
        (since,),
    )
    by_day = [
        {"date": str(r["d"]), "calls": int(r["calls"] or 0), "tokens": int(r["tokens"] or 0)}
        for r in day_rows
    ]

    project_rows = _safe_rows(
        "SELECT u.project_id AS project_id, COALESCE(p.name, '') AS name, "
        "COALESCE(p.owner_id, '') AS owner_id, SUM(u.calls) AS calls, SUM(u.tokens) AS tokens "
        "FROM project_llm_usage u LEFT JOIN projects p ON p.id = u.project_id "
        "WHERE u.usage_date >= ? GROUP BY u.project_id ORDER BY tokens DESC LIMIT 10",
        (since,),
    )
    by_project = [
        {
            "projectId": str(r["project_id"]),
            "name": r["name"],
            "ownerId": r["owner_id"],
            "calls": int(r["calls"] or 0),
            "tokens": int(r["tokens"] or 0),
        }
        for r in project_rows
    ]

    totals = {
        "calls": sum(item["calls"] for item in by_day),
        "tokens": sum(item["tokens"] for item in by_day),
    }
    return ApiResponse(data={"days": max(1, min(int(days or 1), _MAX_DAYS)), "totals": totals, "byDay": by_day, "byProject": by_project})


class InviteCreate(BaseModel):
    label: str = ""
    # 批量发放：一次最多 20 个。明文只在响应里出现一次，所以必须一次给全。
    count: int = Field(default=1, ge=1, le=20)
    maxUses: int = Field(default=1, ge=1, le=100)
    expiresInDays: int = Field(default=7, ge=1, le=365)


@router.get("/invites")
async def admin_invites(_: str = Depends(require_maintainer)) -> ApiResponse:
    try:
        rows = invites_dao.list_invites()
    except Exception:  # noqa: BLE001
        rows = []
    items = [
        {
            "id": row.get("id"),
            "hint": row.get("code_hint"),
            "label": row.get("label") or "",
            "status": row.get("status"),
            "usedCount": int(row.get("used_count") or 0),
            "maxUses": int(row.get("max_uses") or 1),
            "expiresAt": row.get("expires_at") or "",
            "boundUserId": row.get("bound_user_id") or "",
            "boundUsername": row.get("bound_username") or "",
            "createdAt": row.get("created_at") or "",
        }
        for row in rows
    ]
    return ApiResponse(data={"invites": items})


@router.post("/invites")
async def admin_create_invite(payload: InviteCreate, user_id: str = Depends(require_maintainer)) -> ApiResponse:
    """生成邀请码（支持批量）。明文只在此响应里出现一次（库里只存 sha256）。"""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=int(payload.expiresInDays))
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        codes = invites_dao.generate_invites(
            int(payload.count), payload.label, user_id, expires_at=expires_at, max_uses=int(payload.maxUses)
        )
    except Exception as exc:  # noqa: BLE001 - 不把内部异常细节暴露给前端
        raise HTTPException(status_code=500, detail="邀请码生成失败。") from exc
    if not codes:
        raise HTTPException(status_code=500, detail="邀请码生成失败。")
    return ApiResponse(
        data={
            "codes": list(codes),
            "count": len(codes),
            "label": payload.label,
            "maxUses": int(payload.maxUses),
            "expiresAt": expires_at,
            "warning": "明文只显示这一次，请立即复制；库内只存哈希，之后无法查看。",
        }
    )


@router.post("/invites/{invite_id}/disable")
async def admin_disable_invite(invite_id: str, _: str = Depends(require_maintainer)) -> ApiResponse:
    try:
        row = invites_dao.disable_invite(invite_id=invite_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="邀请码不存在。")
    return ApiResponse(data={"id": row.get("id"), "status": row.get("status")})
