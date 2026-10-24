"""Strict one-shot executor for frozen Experiment 001 requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app import db
from app.core.config import settings


CallModel = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _contains_chinese_prose(text: Any) -> bool:
    normalized = str(text or "").strip()
    return bool(normalized) and any("\u4e00" <= character <= "\u9fff" for character in normalized)


def _contains_latin_characters(text: Any) -> bool:
    return bool(re.search(r"[A-Za-z]", str(text or "")))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _cost(usage: dict[str, Any], price: dict[str, Any]) -> tuple[float, str]:
    cached = usage.get("prompt_cache_hit_tokens")
    cache_status = "hit" if isinstance(cached, int) and cached > 0 else "miss_or_unknown"
    hit = int(cached or 0)
    prompt = int(usage["prompt_tokens"])
    output = int(usage["completion_tokens"])
    value = (hit * float(price["cache_hit_input_per_m"]) + (prompt - hit) * float(price["cache_miss_input_per_m"]) + output * float(price["output_per_m"])) / 1_000_000
    return round(value, 6), cache_status


async def _deepseek_call(request: dict[str, Any]) -> dict[str, Any]:
    key = settings.deepseek_api_key
    if not key:
        raise RuntimeError("DeepSeek API key is not configured")
    body = {"model": request["model"], "messages": [{"role": "system", "content": request["system_prompt"]}, {"role": "user", "content": request["user_prompt"]}], "temperature": request["temperature"], "max_tokens": request["max_tokens"], "thinking": {"type": request["thinking_mode"]}, "reasoning_effort": request["reasoning_effort"]}
    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body)
        response.raise_for_status()
        data = response.json()
    return {"text": data["choices"][0]["message"].get("content") or "", "usage": data.get("usage") or {}}


async def execute_experiment_001(artifact_root: Path, *, call_model: CallModel | None = None) -> dict[str, Any]:
    manifest = json.loads((artifact_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "prepared_do_not_run" or (manifest.get("cost_preflight") or {}).get("blocked"):
        raise RuntimeError("Experiment 001 is not ready")
    fixed = manifest["fixed_parameters"]
    if fixed["provider"] != "DeepSeek" or fixed["model"] != "deepseek-v4-flash":
        raise RuntimeError("frozen provider/model is not DeepSeek V4 Flash")
    requests = json.loads((artifact_root / "frozen_requests.json").read_text(encoding="utf-8"))
    by_key = {(row["chapter_id"], variant): row for variant, rows in requests.items() for row in rows}
    output = artifact_root / "execution"
    for name in ("outputs", "commits", "projection", "memory", "audit", "results"):
        (output / name).mkdir(parents=True, exist_ok=True)
    production_before = _hash(Path(db.DB_PATH))
    caller = call_model or _deepseek_call
    calls: list[dict[str, Any]] = []
    status = "COMPLETED_PENDING_HUMAN_REVIEW"
    for position, item in enumerate(manifest["run_order"], start=1):
        variant, chapter_id = item["variant"], item["chapter_id"]
        frozen = by_key[(chapter_id, variant)]
        request = {**frozen, **fixed, "variant": variant}
        started = time.monotonic()
        try:
            response = await caller(request)
            usage = response.get("usage") or {}
            text = response.get("text")
            if not _contains_chinese_prose(text):
                raise RuntimeError("model response does not contain Chinese prose")
            if _contains_latin_characters(text):
                raise RuntimeError("model response contains Latin characters")
            if not all(key in usage for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
                raise RuntimeError("missing prose or usage audit")
            actual_cost, cache_status = _cost(usage, manifest["price_config"])
            row = {"position": position, "chapter_id": chapter_id, "variant": variant, "status": "success", "elapsed_s": round(time.monotonic() - started, 3), "estimated_input_tokens": frozen["estimated_input_tokens"], "actual_input_tokens": int(usage["prompt_tokens"]), "actual_output_tokens": int(usage["completion_tokens"]), "actual_total_tokens": int(usage["total_tokens"]), "cache_hit": usage.get("prompt_cache_hit_tokens"), "cache_status": cache_status, "estimated_cost": None, "actual_cost": actual_cost, "price_schedule": manifest["price_config"]["price_schedule"]}
            (output / "outputs" / f"{position:02d}-{variant}-{chapter_id}.txt").write_text(text, encoding="utf-8")
            calls.append(row)
            if sum(call["actual_cost"] for call in calls) >= 5.0:
                status = "PAUSED_COST_TARGET"
                break
        except Exception as exc:
            calls.append({"position": position, "chapter_id": chapter_id, "variant": variant, "status": "failed", "error": str(exc), "elapsed_s": round(time.monotonic() - started, 3)})
            status = "INCOMPLETE"
            break
    production_after = _hash(Path(db.DB_PATH))
    if production_before != production_after:
        status = "FAILED_PRODUCTION_DB_MODIFIED"
    result = {"experiment_id": "Experiment-001", "status": status, "actual_calls": len(calls), "retries": 0, "rewrites": 0, "production_db_hash_before": production_before, "production_db_hash_after": production_after, "calls": calls, "artifacts": {"results": str(output / "results" / "experiment-001-results.json")}}
    Path(result["artifacts"]["results"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "audit" / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
