"""llm_configs 表 DAO（Data Access Object）—— 封装所有对 llm_configs 表的 SQL 操作。"""

from __future__ import annotations

from typing import Any

from app.dao.constants import TABLE_LLM_CONFIGS
from app.db import get_conn, row_to_llm_config


def list_configs() -> list[dict[str, Any]]:
    """获取全部 LLM 配置，按更新时间倒序排列。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM {TABLE_LLM_CONFIGS} ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()
    return [row_to_llm_config(row) for row in rows]


def get_config(config_id: str) -> dict[str, Any] | None:
    """按主键查询单个 LLM 配置。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_LLM_CONFIGS} WHERE id = ?", (config_id,)
        ).fetchone()
    return row_to_llm_config(row) if row else None


def get_active_config() -> dict[str, Any] | None:
    """获取当前激活的 LLM 配置（is_active=1 的最新一条）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_LLM_CONFIGS} WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return row_to_llm_config(row) if row else None


def create_config(fields: dict[str, Any]) -> None:
    """插入一条 LLM 配置记录。
    
    fields 字典的键为数据库列名（snake_case），值已序列化。
    """
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    values = tuple(fields[col] for col in columns)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO {TABLE_LLM_CONFIGS} ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )


def deactivate_all_configs() -> None:
    """将所有 LLM 配置设为非激活状态。"""
    with get_conn() as conn:
        conn.execute(f"UPDATE {TABLE_LLM_CONFIGS} SET is_active = 0")


def activate_config(config_id: str, today: str) -> None:
    """激活指定 LLM 配置（不自动 deactivate 其他配置）。"""
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {TABLE_LLM_CONFIGS} SET is_active = 1, updated_at = ? WHERE id = ?",
            (today, config_id),
        )


def delete_config(config_id: str) -> int:
    """删除 LLM 配置记录，返回受影响行数。"""
    with get_conn() as conn:
        result = conn.execute(
            f"DELETE FROM {TABLE_LLM_CONFIGS} WHERE id = ?", (config_id,)
        )
        return result.rowcount


def get_next_active_candidate() -> dict[str, Any] | None:
    """获取除当前激活配置外的最新一条配置（用于激活配置被删除时自动切换）。"""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM {TABLE_LLM_CONFIGS} ORDER BY updated_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
    return row_to_llm_config(row) if row else None


def update_role_overrides(config_id: str, role_overrides_json: str, today: str) -> None:
    """更新指定配置的角色级模型覆盖（建议4 模型档位路由）。"""
    with get_conn() as conn:
        conn.execute(
            f"UPDATE {TABLE_LLM_CONFIGS} SET role_overrides = ?, updated_at = ? WHERE id = ?",
            (role_overrides_json, today, config_id),
        )
