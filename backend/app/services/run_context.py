"""Context-local run identity for Beta, Experiment, and test operations."""

from __future__ import annotations

import contextvars
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("novelcraft_run_id", default="")


def start_run(run_id: str | None = None) -> str:
    value = str(run_id or uuid.uuid4().hex)
    _RUN_ID.set(value)
    return value


def begin_run(run_id: str | None = None) -> tuple[str, contextvars.Token]:
    """建立本次请求/任务的 run 标识，返回 (run_id, token)。

    调用方必须在 finally 中用 ``end_run(token)`` 恢复，避免 run 上下文泄漏到
    同一任务中后续不相关的调用（contextvar 是协程局部的，但显式恢复更安全）。
    """
    value = str(run_id or uuid.uuid4().hex)
    return value, _RUN_ID.set(value)


def end_run(token: contextvars.Token | None) -> None:
    if token is not None:
        try:
            _RUN_ID.reset(token)
        except ValueError:
            pass


def get_run_id() -> str:
    return _RUN_ID.get() or ""


def clear_run() -> None:
    _RUN_ID.set("")


def write_run_manifest(
    path: str | Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking: Any = None,
    reasoning: Any = None,
    environment: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Write a secret-free manifest for the current beta/experiment run."""
    manifest = {
        "run_id": get_run_id() or start_run(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": thinking,
        "reasoning": reasoning,
        "environment": environment,
        "database": database,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
