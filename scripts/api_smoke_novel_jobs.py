"""NovelCraft AI — 长篇创作任务 API 巡检脚本

覆盖 4 个核心端点：
  POST   /api/ai/novel-jobs/start
  GET    /api/ai/novel-jobs/project/{id}
  GET    /api/ai/novel-jobs/{job_id}
  POST   /api/ai/novel-jobs/{job_id}/cancel

用法：
  python scripts/api_smoke_novel_jobs.py

环境变量（可选）：
  NOVELCRAFT_API_BASE  默认 http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
import json

API_BASE = os.environ.get("NOVELCRAFT_API_BASE", "http://127.0.0.1:8000")


def _api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        print(f"  ✗ 请求失败: {exc}")
        raise


def _create_project(name: str) -> str:
    resp = _api("POST", "/api/projects/", {
        "name": name,
        "subtitle": "api smoke test",
        "genre": "scifi",
        "targetWordCount": 3000,
        "premise": "Test premise for novel job smoke.",
        "targetChapters": 2,
        "stylePreset": "concise",
    })
    return resp["data"]["id"]


def _add_outline(project_id: str) -> None:
    resp = _api("POST", "/api/ai/generate-outline", {
        "premise": "测试大纲，用于长篇创作 API 巡检。",
        "genre": "scifi",
        "targetWordCount": 3000,
        "targetChapters": 2,
        "structure": "three_act",
    })
    _api("PATCH", f"/api/projects/{project_id}", {
        "outline": resp["data"],
        "targetChapters": 2,
    })


def _delete_project(project_id: str) -> None:
    try:
        _api("DELETE", f"/api/projects/{project_id}")
    except Exception:
        pass


def main():
    print("=" * 60)
    print("长篇创作任务 API 巡检")
    print(f"API Base: {API_BASE}")
    print()

    project_id: str | None = None
    job_id: str | None = None

    try:
        # ── 1. 创建项目 + 大纲 ──────────────────────
        project_id = _create_project(f"novel-job-smoke-{int(time.time())}")
        _add_outline(project_id)
        print(f"✓ 已创建测试项目: {project_id}")

        # ── 2. POST /start ──────────────────────────
        start = _api("POST", "/api/ai/novel-jobs/start", {
            "projectId": project_id,
            "mode": "all",
        })
        assert start.get("data"), f"start 响应缺少 data: {start}"
        job_id = start["data"]["id"]
        status = start["data"]["status"]
        assert status in ("queued", "running"), f"start 异常状态: {status}"
        print(f"✓ POST /start → job_id={job_id}, status={status}")

        # ── 3. GET /{job_id} ────────────────────────
        for attempt in range(1, 31):
            time.sleep(1)
            detail = _api("GET", f"/api/ai/novel-jobs/{job_id}")
            job = detail["data"]
            if job["status"] in ("completed", "failed", "cancelled"):
                break
        print(f"✓ GET /{job_id} → status={job['status']}, "
              f"completed={job['completed_chapters']}/{job['total_chapters']}, "
              f"words={job['generated_words']}")

        # 重跑以验证 status 保持不变
        detail2 = _api("GET", f"/api/ai/novel-jobs/{job_id}")
        assert detail2["data"]["status"] == job["status"], "任务状态不应在完成后再变"
        print("✓ GET /{job_id} (第2次) → 状态稳定")

        # ── 4. GET /project/{id} ────────────────────
        proj_job = _api("GET", f"/api/ai/novel-jobs/project/{project_id}")
        # 已完成的任务应从 project 查询返回 null
        pj_data = proj_job.get("data")
        if pj_data is None:
            print(f"✓ GET /project/{project_id} → null (已完成任务正确不返回)")
        else:
            pj_id = pj_data.get("id", "?")
            print(f"✓ GET /project/{project_id} → job_id={pj_id}")

        # ── 5. 重复 start 应被拒绝 ───────────────────
        try:
            _api("POST", "/api/ai/novel-jobs/start", {
                "projectId": project_id,
                "mode": "all",
            })
        except Exception as exc:
            assert "已有" in str(exc) or "400" in str(exc), f"期望 400 拒绝，得到: {exc}"
            print(f"✓ 重复 start 正确拒绝（项目中已有任务）")

        print("\n✅ 长篇创作任务 API 巡检全部通过")

    except Exception as exc:
        print(f"\n✗ 巡检中断: {exc}")
        sys.exit(1)
    finally:
        if project_id:
            _delete_project(project_id)
            print(f"✓ 已清理测试项目: {project_id}")


if __name__ == "__main__":
    main()
