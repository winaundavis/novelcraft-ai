"""Prepare the Phase 7-D ten-chapter experiment without model calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.phase7d_experiment_service import prepare_phase7d_experiment


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="test_artifacts")
    parser.add_argument("--provider", default="DeepSeek")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--price-source", required=True)
    parser.add_argument("--price-checked-at", required=True)
    parser.add_argument("--price-schedule", choices=("off_peak", "peak"), default="peak")
    parser.add_argument("--cache-hit-input-per-m", type=float, default=0.10)
    parser.add_argument("--cache-miss-input-per-m", type=float, default=3.00)
    parser.add_argument("--output-per-m", type=float, default=9.00)
    parser.add_argument("--thinking-mode", default="disabled")
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    result = prepare_phase7d_experiment(
        Path(args.output_dir),
        provider=args.provider,
        model=args.model,
        price_config={
            "currency": "CNY",
            "price_source": args.price_source,
            "price_checked_at": args.price_checked_at,
            "price_schedule": args.price_schedule,
            "cache_hit_input_per_m": args.cache_hit_input_per_m,
            "cache_miss_input_per_m": args.cache_miss_input_per_m,
            "output_per_m": args.output_per_m,
        },
        thinking_mode=args.thinking_mode,
        reasoning_effort=args.reasoning_effort,
        finalize=args.finalize,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
