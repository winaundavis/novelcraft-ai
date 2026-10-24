# -*- coding: utf-8 -*-
"""LLM 调用审计（Codex 六轮 P1：E2E 报告必须为每个 live-LLM 用例提供真实模型证据）。

设计：
- `llm_audit_log` 表记录**每次真实 LLM 调用**：provider / model / source(llm|fallback) /
  status(success|timeout|error|circuit_open) / duration_ms / role / project_id / job_id /
  created_at。证据由 provider 层在真实 HTTP 调用成功/失败时写入。
- service 层在调用 `llm.chat`/`chat_stream` 前通过 contextvar 设置审计上下文
  （project_id / job_id / role），provider 层读取后随记录落库。
- 前端 live-LLM 用例从 `GET /api/llm/audit` 拉取本次用例触发的审计记录，
  写入 `llm-evidence` annotation；报告按此验证"每个 live 用例有真实成功证据"。
"""
import contextvars
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from app.models.schemas import gen_id
from app.core.config import resolve_data_dir, resolve_database_path, settings
from app.services import run_context

DATA_DIR = resolve_data_dir(settings.data_dir)
DB_PATH = resolve_database_path(DATA_DIR, settings.novelcraft_db_path or os.getenv("NOVELCRAFT_DB_PATH", ""))

# 当前 LLM 调用的审计上下文（project_id / job_id / role），service 层调用前设置
_AUDIT_CTX: contextvars.ContextVar = contextvars.ContextVar("llm_audit_ctx", default={})


def set_llm_audit_context(project_id: str = "", job_id: str = "", role: str = "", chapter_id: str = "", operation: str = "", budget_dimension: str = "", budget_before: int | None = None, budget_after: int | None = None, merge: bool = True) -> contextvars.Token | None:
    """设置当前（协程/任务）内 LLM 调用的审计上下文。

    merge=True（默认）：只覆盖非空字段，保留已有 project_id/job_id——
    避免 job 后台任务内部调用 service 时被请求级 project_id 覆盖掉 job 关联。
    返回 contextvar token，调用方在 finally 中用 reset_llm_audit_context(token)
    恢复调用前的上下文（用于嵌套场景：job 外层已设置 context 时内层修改不污染外层）。
    """
    ctx = _AUDIT_CTX.get() if merge else {}
    new_ctx = {
        "project_id": project_id or ctx.get("project_id", ""),
        "job_id": job_id or ctx.get("job_id", ""),
        "role": role or ctx.get("role", ""),
        "chapter_id": chapter_id or ctx.get("chapter_id", ""),
        "operation": operation or ctx.get("operation", ""),
        "budget_dimension": budget_dimension or ctx.get("budget_dimension", ""),
        "budget_before": budget_before if budget_before is not None else ctx.get("budget_before"),
        "budget_after": budget_after if budget_after is not None else ctx.get("budget_after"),
    }
    return _AUDIT_CTX.set(new_ctx)


def reset_llm_audit_context(token: contextvars.Token | None) -> None:
    """恢复调用前的审计上下文（配合 set_llm_audit_context 的 token）。"""
    if token is not None:
        try:
            _AUDIT_CTX.reset(token)
        except ValueError:
            pass


def clear_llm_audit_context() -> None:
    _AUDIT_CTX.set({})


def get_llm_audit_context() -> dict:
    return dict(_AUDIT_CTX.get() or {})


# 最近一次实际 LLM 调用元数据（Codex 八轮 P1-1）：provider/model/audit_id。
# 用 contextvar（协程隔离）而非模块级共享变量——避免并发请求串线。
_CALL_META_CTX: contextvars.ContextVar = contextvars.ContextVar("llm_call_meta", default={})


def set_llm_call_meta(provider: str = "", model: str = "", audit_id: str = "", finish_reason: str = "") -> None:
    """记录最近一次实际 LLM 调用的 provider / model / audit_id（协程隔离）。"""
    _CALL_META_CTX.set(
        {
            "provider": provider or "",
            "model": model or "",
            "audit_id": audit_id or "",
            "finish_reason": finish_reason or "",
        }
    )


def get_llm_call_meta() -> dict:
    """读取最近一次实际 LLM 调用的 provider / model / audit_id。"""
    return dict(_CALL_META_CTX.get() or {})


def clear_llm_call_meta() -> None:
    _CALL_META_CTX.set({})


def _ensure_table() -> None:
    """幂等建表（审计写入不应因表缺失而阻塞主流程）。

    自 Codex 八轮起：`llm_audit_log` 额外记录请求/响应**脱敏元数据**列——
    message_count / input_chars / est_tokens / max_tokens / temperature（请求侧），
    http_status / finish_reason / prompt_tokens / completion_tokens / total_tokens /
    reasoning_chars / request_id（响应侧）。正文与密钥绝不落库。
    """
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_audit_log (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'llm',
                    status TEXT NOT NULL DEFAULT 'success',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    output_chars INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    input_chars INTEGER NOT NULL DEFAULT 0,
                    est_tokens INTEGER NOT NULL DEFAULT 0,
                    max_tokens INTEGER NOT NULL DEFAULT 0,
                    temperature REAL NOT NULL DEFAULT 0,
                    http_status INTEGER NOT NULL DEFAULT 0,
                    finish_reason TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    -- 缓存命中 token：NULL = provider 未回传（不得推断为 0）
                    cache_hit_tokens INTEGER,
                    reasoning_chars INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT NOT NULL DEFAULT ''
                    , chapter_id TEXT NOT NULL DEFAULT ''
                    , operation TEXT NOT NULL DEFAULT ''
                    , budget_dimension TEXT NOT NULL DEFAULT ''
                    , budget_before INTEGER
                    , budget_after INTEGER
                    , run_id TEXT NOT NULL DEFAULT ''
                    , postprocess_status TEXT NOT NULL DEFAULT ''
                    , postprocess_stage TEXT NOT NULL DEFAULT ''
                    , postprocess_code TEXT NOT NULL DEFAULT ''
                    , response_shape TEXT NOT NULL DEFAULT ''
                    , parsed_node_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 兼容已存在的旧表：幂等补列（老库无新列时 ALTER 增加）
            existing = {r[1] for r in con.execute("PRAGMA table_info(llm_audit_log)").fetchall()}
            for col_sql in (
                "message_count INTEGER NOT NULL DEFAULT 0",
                "input_chars INTEGER NOT NULL DEFAULT 0",
                "est_tokens INTEGER NOT NULL DEFAULT 0",
                "max_tokens INTEGER NOT NULL DEFAULT 0",
                "temperature REAL NOT NULL DEFAULT 0",
                "http_status INTEGER NOT NULL DEFAULT 0",
                "finish_reason TEXT NOT NULL DEFAULT ''",
                "prompt_tokens INTEGER NOT NULL DEFAULT 0",
                "completion_tokens INTEGER NOT NULL DEFAULT 0",
                "total_tokens INTEGER NOT NULL DEFAULT 0",
                "cache_hit_tokens INTEGER",
                "reasoning_chars INTEGER NOT NULL DEFAULT 0",
                "request_id TEXT NOT NULL DEFAULT ''",
                "chapter_id TEXT NOT NULL DEFAULT ''",
                "operation TEXT NOT NULL DEFAULT ''",
                "budget_dimension TEXT NOT NULL DEFAULT ''",
                "budget_before INTEGER",
                "budget_after INTEGER",
                "run_id TEXT NOT NULL DEFAULT ''",
                "postprocess_status TEXT NOT NULL DEFAULT ''",
                "postprocess_stage TEXT NOT NULL DEFAULT ''",
                "postprocess_code TEXT NOT NULL DEFAULT ''",
                "response_shape TEXT NOT NULL DEFAULT ''",
                "parsed_node_count INTEGER NOT NULL DEFAULT 0",
            ):
                col = col_sql.split()[0]
                if col not in existing:
                    con.execute(f"ALTER TABLE llm_audit_log ADD COLUMN {col_sql}")
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def record_llm_call(
    *,
    provider: str,
    model: str,
    source: str = "llm",
    status: str = "success",
    duration_ms: int = 0,
    output_chars: int = 0,
    project_id: str = "",
    job_id: str = "",
    role: str = "",
    # 请求/响应脱敏元数据（Codex 八轮 DeepSeek 诊断）：
    message_count: int = 0,
    input_chars: int = 0,
    est_tokens: int = 0,
    max_tokens: int = 0,
    temperature: float = 0.0,
    http_status: int = 0,
    finish_reason: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cache_hit_tokens: int | None = None,
    reasoning_chars: int = 0,
    request_id: str = "",
    chapter_id: str = "",
    operation: str = "",
    budget_dimension: str = "",
    budget_before: int | None = None,
    budget_after: int | None = None,
) -> str | None:
    """记录一次真实 LLM 调用，返回审计记录 id（写入失败返回 None，不阻塞主流程）。

    只接收脱敏指标/元数据；消息正文与 API 密钥绝不落库。
    """
    ctx = get_llm_audit_context()
    run_id_value = run_context.get_run_id() or ctx.get("run_id", "") or gen_id()
    row = {
        "id": gen_id(),
        "project_id": project_id or ctx.get("project_id", ""),
        "job_id": job_id or ctx.get("job_id", ""),
        "role": role or ctx.get("role", ""),
        "provider": provider or "",
        "model": model or "",
        "source": source,
        "status": status,
        "duration_ms": int(duration_ms),
        "output_chars": int(output_chars),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "message_count": int(message_count),
        "input_chars": int(input_chars),
        "est_tokens": int(est_tokens),
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "http_status": int(http_status),
        "finish_reason": finish_reason or "",
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        # provider 未回传缓存命中量为 NULL（= 未知），不伪造成 0。
        "cache_hit_tokens": int(cache_hit_tokens) if cache_hit_tokens is not None else None,
        "reasoning_chars": int(reasoning_chars),
        "request_id": request_id or "",
        "chapter_id": chapter_id or ctx.get("chapter_id", ""),
        "operation": operation or ctx.get("operation", ""),
        "budget_dimension": budget_dimension or ctx.get("budget_dimension", ""),
        "budget_before": budget_before if budget_before is not None else ctx.get("budget_before"),
        "budget_after": budget_after if budget_after is not None else ctx.get("budget_after"),
        "run_id": run_id_value,
    }
    try:
        _ensure_table()
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute(
                """
                INSERT INTO llm_audit_log
                (id, project_id, job_id, role, provider, model, source, status,
                 duration_ms, output_chars, created_at,
                 message_count, input_chars, est_tokens, max_tokens, temperature,
                 http_status, finish_reason, prompt_tokens, completion_tokens,
                 total_tokens, cache_hit_tokens, reasoning_chars, request_id, chapter_id, operation, budget_dimension, budget_before, budget_after, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["project_id"], row["job_id"], row["role"],
                    row["provider"], row["model"], row["source"], row["status"],
                    row["duration_ms"], row["output_chars"], row["created_at"],
                    row["message_count"], row["input_chars"], row["est_tokens"],
                    row["max_tokens"], row["temperature"], row["http_status"],
                    row["finish_reason"], row["prompt_tokens"], row["completion_tokens"],
                    row["total_tokens"], row["cache_hit_tokens"], row["reasoning_chars"], row["request_id"],
                    row["chapter_id"], row["operation"], row["budget_dimension"], row["budget_before"], row["budget_after"], row["run_id"],
                ),
            )
            con.commit()
        finally:
            con.close()
        return row["id"]
    except Exception:
        return None


def record_llm_postprocess(
    audit_id: str,
    *,
    status: str,
    stage: str = "",
    code: str = "",
    response_shape: str = "",
    parsed_node_count: int = 0,
) -> bool:
    """Attach content-free post-processing diagnostics to one LLM audit row."""
    if not audit_id:
        return False
    try:
        _ensure_table()
        con = sqlite3.connect(DB_PATH)
        try:
            result = con.execute(
                """
                UPDATE llm_audit_log
                SET postprocess_status = ?, postprocess_stage = ?, postprocess_code = ?,
                    response_shape = ?, parsed_node_count = ?
                WHERE id = ?
                """,
                (
                    str(status or "")[:40],
                    str(stage or "")[:40],
                    str(code or "")[:80],
                    str(response_shape or "")[:80],
                    max(0, int(parsed_node_count or 0)),
                    audit_id,
                ),
            )
            con.commit()
            return result.rowcount == 1
        finally:
            con.close()
    except Exception:
        return False


def get_llm_audit_record(audit_id: str) -> dict | None:
    """按 id 读取单条审计记录（用于读取本次调用的真实 usage；缺失返回 None）。"""
    if not audit_id:
        return None
    try:
        _ensure_table()
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT * FROM llm_audit_log WHERE id = ?", (audit_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()
    except Exception:
        return None


def query_llm_audit(
    *,
    since: str = "",
    minutes: int | None = None,
    project_id: str = "",
    job_id: str = "",
    role: str = "",
    run_id: str = "",
    scope: str = "run",
    limit: int = 200,
) -> list[dict]:
    """查询审计记录（按 created_at 倒序）。支持 since（ISO 起）或 minutes（最近 N 分钟）。

    `scope` 决定 run 维度：
    - ``"run"``（默认）：只返回**当前 run**（显式 run_id 或请求/任务上下文）的记录；
      没有 run 上下文时明确返回空列表，而不是隐式返回全表。
    - ``"all"``：显式命名的全量范围（维护者统计/交付验收），必须由调用方主动指定。

    历史 run 通过显式 run_id 查询；time / project / job / role / run 过滤同时生效。
    每行附带 ``run_scope`` 标注：current / other / legacy（无 run_id 的旧记录）。
    注意：项目日预算（project_daily_llm_call_limit / token_limit）是项目＋日期维度，
    与 run 隔离无关，本函数不参与预算计算。
    """
    try:
        _ensure_table()
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            where = []
            params: list = []
            active_run_id = run_id or run_context.get_run_id()
            if scope != "all":
                if not active_run_id:
                    # 无 run 上下文：明确返回空，避免把不同 Beta/Experiment/Test run 混算。
                    return []
                where.append("run_id = ?")
                params.append(active_run_id)
            elif run_id:
                where.append("run_id = ?")
                params.append(run_id)
            if since:
                where.append("created_at >= ?")
                params.append(since)
            elif minutes is not None:
                cutoff = time.time() - max(1, int(minutes)) * 60
                cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                where.append("created_at >= ?")
                params.append(cutoff_iso)
            if project_id:
                where.append("project_id = ?")
                params.append(project_id)
            if job_id:
                where.append("job_id = ?")
                params.append(job_id)
            if role:
                where.append("role = ?")
                params.append(role)
            sql = "SELECT * FROM llm_audit_log"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(max(1, min(int(limit), 1000)))
            rows = con.execute(sql, params).fetchall()
            result = [dict(r) for r in rows]
            for item in result:
                # Providers that omit usage are represented as unknown, never as
                # an invented zero-token response.
                item["actual_tokens"] = int(item["total_tokens"]) if int(item.get("total_tokens") or 0) > 0 else None
                # 明确标注 run 归属：旧记录（无 run_id）标为 legacy，不重写历史 run ID。
                row_run = str(item.get("run_id") or "")
                if not row_run:
                    item["run_scope"] = "legacy"
                elif active_run_id and row_run == active_run_id:
                    item["run_scope"] = "current"
                else:
                    item["run_scope"] = "other"
            return result
        finally:
            con.close()
    except Exception:
        return []
