#!/usr/bin/env python3
"""Report target-vs-actual word counts for a project's chapters."""

from __future__ import annotations

import argparse
import json
from urllib.request import Request, urlopen

API_BASE = "http://127.0.0.1:8000/api"


def api(path: str) -> dict:
    request = Request(f"{API_BASE}{path}", method="GET")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    args = parser.parse_args()

    project = api(f"/projects/{args.project_id}")["data"]
    chapters = api(f"/projects/{args.project_id}/chapters/")["data"]
    outline = project.get("outline") or {}
    nodes = {
        node["id"]: node
        for node in outline.get("nodes", [])
        if node.get("type") == "chapter"
    }

    print(f"Project: {project['name']} ({args.project_id})")
    print(f"Target words: {project.get('targetWordCount', 0)}")
    print(f"Current words: {project.get('currentWordCount', 0)}")
    print("-" * 96)
    print(f"{'Order':>5}  {'Title':<24} {'Target':>8} {'Actual':>8} {'Ratio':>7}")
    print("-" * 96)

    total_target = 0
    total_actual = 0
    for chapter in sorted(chapters, key=lambda item: item.get("order", 0)):
        node = nodes.get(chapter["id"], {})
        target = int(node.get("targetWordCount") or 0)
        actual = int(chapter.get("wordCount") or 0)
        total_target += target
        total_actual += actual
        ratio = (actual / target) if target else 0
        title = (chapter.get("title") or "")[:24]
        print(f"{chapter.get('order', 0):>5}  {title:<24} {target:>8} {actual:>8} {ratio:>7.1%}")

    print("-" * 96)
    overall = (total_actual / total_target) if total_target else 0
    print(f"{'SUM':>5}  {'':<24} {total_target:>8} {total_actual:>8} {overall:>7.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
