"""Create a no-provider-call Chapter 1 request manifest for Phase 10-F."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import init_db
from app.services.beta_smoke_manifest_service import build_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a secret-free Beta Chapter 1 dry-run manifest.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--chapter-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    init_db()
    manifest = build_manifest(args.project_id, args.chapter_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
