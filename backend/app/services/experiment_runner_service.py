"""Experiment 001 dry-run manifest.  It creates no project records and calls no LLM."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict
from typing import Any

from app.services.experiment_schema import REVIEW_DIMENSIONS


REQUIRED = ("provider", "model", "temperature", "max_tokens", "story", "characters", "outline", "target_length")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def dry_run_experiment_001(baseline: dict[str, Any], candidate: dict[str, Any], *, seed: int = 1) -> dict[str, Any]:
    missing = [key for key in REQUIRED if key not in baseline or key not in candidate]
    if missing:
        raise ValueError(f"missing fixed experiment fields: {', '.join(missing)}")
    changed = [key for key in REQUIRED if baseline[key] != candidate[key]]
    if changed:
        raise ValueError(f"variants differ outside harness: {', '.join(changed)}")
    if baseline.get("harness") == candidate.get("harness"):
        raise ValueError("baseline and candidate harnesses must differ")
    rng = random.Random(seed)
    labels = ["A", "B"]
    rng.shuffle(labels)
    audit = []
    for variant, config in (("baseline", baseline), ("candidate", candidate)):
        audit.append({"experiment_id": "Experiment-001", "variant": variant, "chapter": None,
                      "model": config["model"], "prompt_hash": _hash(config.get("harness")),
                      "context_hash": _hash({key: config[key] for key in REQUIRED}), "tokens": None,
                      "cost": None, "latency": None, "retry": 0, "rewrite": 0})
    return {"experiment_id": "Experiment-001", "status": "prepared_do_not_run", "legacy_status": "dry_run_passed", "real_llm_calls": 0,
            "fixed_fields": list(REQUIRED), "blind_labels": {labels[0]: "baseline", labels[1]: "candidate"},
            "review_dimensions": [*REVIEW_DIMENSIONS, "overall_preference"], "audit": audit}
