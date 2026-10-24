"""DAO 层统一入口 —— 导出所有数据访问对象及事务辅助工具。"""

from __future__ import annotations

from contextlib import contextmanager

import sqlite3

from app.db import get_conn

# ── DAO 模块导出 ──────────────────────────────────────
from app.dao import projects as projects_dao
from app.dao import chapters as chapters_dao
from app.dao import llm_configs as llm_configs_dao
from app.dao import novel_jobs as novel_jobs_dao
from app.dao import novel_facts as novel_facts_dao
from app.dao import volumes as volumes_dao

# 便捷别名
projects = projects_dao
chapters = chapters_dao
llm_configs = llm_configs_dao
novel_jobs = novel_jobs_dao
novel_facts = novel_facts_dao
volumes = volumes_dao


# ── 事务辅助 ──────────────────────────────────────────
@contextmanager
def transaction():
    """事务辅助上下文管理器。
    
    在 with 块内使用同一个数据库连接执行多条 SQL；
    块正常结束时自动 commit，发生异常时自动 rollback。
    
    用法：
        from app.dao import transaction
        
        with transaction() as conn:
            conn.execute("INSERT INTO ...", ...)
            conn.execute("UPDATE ...", ...)
            # 正常结束 → commit；抛出异常 → rollback
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
