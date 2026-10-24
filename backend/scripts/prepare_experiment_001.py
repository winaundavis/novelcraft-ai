"""Prepare Experiment 001 artifacts. This command never imports an LLM provider."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.experiment_001_service import prepare_experiment_001


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="test_artifacts")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--price-source")
    parser.add_argument("--price-checked-at")
    parser.add_argument("--price-schedule", choices=("off_peak", "peak"))
    parser.add_argument("--cache-hit-input-per-m", type=float)
    parser.add_argument("--cache-miss-input-per-m", type=float)
    parser.add_argument("--output-per-m", type=float)
    parser.add_argument("--thinking-mode", default="disabled")
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    price_values = (args.price_source, args.price_checked_at, args.price_schedule, args.cache_hit_input_per_m, args.cache_miss_input_per_m, args.output_per_m)
    if any(value is not None for value in price_values) and not all(value is not None for value in price_values):
        parser.error("all CNY price fields are required when setting a price configuration")
    price_config = None if not any(value is not None for value in price_values) else {
        "currency": "CNY",
        "price_source": args.price_source,
        "price_checked_at": args.price_checked_at,
        "price_schedule": args.price_schedule,
        "cache_hit_input_per_m": args.cache_hit_input_per_m,
        "cache_miss_input_per_m": args.cache_miss_input_per_m,
        "output_per_m": args.output_per_m,
    }
    result = prepare_experiment_001(
        Path(args.output_dir), provider=args.provider, model=args.model,
        price_config=price_config,
        thinking_mode=args.thinking_mode,
        reasoning_effort=args.reasoning_effort,
        seed=args.seed,
        finalize=args.finalize,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
