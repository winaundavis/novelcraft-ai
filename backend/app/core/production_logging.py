"""Metadata-only production request logging."""

from __future__ import annotations

import json
import logging


def build_request_event(
    *,
    request_id: str,
    user_id: str,
    project_id: str,
    operation: str,
    status: int,
    latency_ms: float,
    detail: str = "",
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "user_id": user_id or "anonymous",
        "project_id": project_id,
        "operation": operation,
        "status": status,
        "latency_ms": round(latency_ms, 2),
        "usage": None,
        "cost": None,
        "detail": "redacted" if detail else "",
    }


def configure_production_logging(level: str) -> logging.Logger:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    return logging.getLogger("novelcraft.request")


def log_request(logger: logging.Logger, event: dict[str, object]) -> None:
    logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
