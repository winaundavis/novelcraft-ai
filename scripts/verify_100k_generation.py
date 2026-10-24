#!/usr/bin/env python3
"""NovelCraft AI 10万字长篇生成验证脚本

用途：
1. 创建一个 10 万字项目
2. 生成并挂载 24 章大纲
3. 启动长篇创作任务
4. 轮询任务状态，输出真实模型/回退章节/累计字数

建议：
- 请在“用户本机原生终端”运行，而不是受限沙箱环境
- 运行前先确认后端已启动，并且 /api/settings/llm-health 显示连通正常

用法：
  python scripts/verify_100k_generation.py
  python scripts/verify_100k_generation.py --wait-seconds 900
  python scripts/verify_100k_generation.py --keep-project
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "http://127.0.0.1:8000/api"


def api(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(f"{API_BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class RunContext:
    project_id: str
    job_id: str


def create_project() -> str:
    payload = {
        "name": f"verify-100k-{int(time.time())}",
        "subtitle": "原生长篇验证任务",
        "genre": "scifi",
        "targetWordCount": 100000,
        "premise": "一名档案修复师发现，自己的过去正在被陌生人逐章改写，而每次修补都会改变现实中的一处细节。",
        "targetChapters": 24,
        "stylePreset": "epic",
    }
    response = api("POST", "/projects/", payload)
    return response["data"]["id"]


def attach_outline(project_id: str) -> None:
    outline = api(
        "POST",
        "/ai/generate-outline",
        {
            "premise": "一名档案修复师发现，自己的过去正在被陌生人逐章改写，而每次修补都会改变现实中的一处细节。",
            "genre": "mystery",
            "targetWordCount": 100000,
            "targetChapters": 24,
            "structure": "three_act",
            "additionalNotes": "这是一次真实的10万字验证，请保证章节冲突递进、伏笔回收、人物选择持续推动主线。",
        },
        timeout=300,  # kimi 生成大纲实测 ~91s，超时给足 300s
    )
    api("PATCH", f"/projects/{project_id}", {"outline": outline["data"]}, timeout=60)


def start_job(project_id: str) -> str:
    response = api(
        "POST",
        "/ai/start",
        {"projectId": project_id, "mode": "all", "maxRetries": args_namespace.max_retries},
        timeout=30,
    )
    return response["data"]["id"]


def delete_project(project_id: str) -> None:
    try:
        api("DELETE", f"/projects/{project_id}", timeout=30)
    except Exception:
        pass


def cancel_job(job_id: str) -> None:
    try:
        api("POST", f"/ai/{job_id}/cancel", timeout=30)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-seconds", type=int, default=180, help="最长等待秒数，默认 180")
    parser.add_argument("--poll-seconds", type=int, default=10, help="轮询间隔，默认 10 秒")
    parser.add_argument("--keep-project", action="store_true", help="结束后保留项目，不自动删除")
    parser.add_argument("--require-real-llm", action="store_true", help="要求模型连通性正常，否则直接失败")
    parser.add_argument("--max-retries", type=int, default=2, help="每章生成失败时的最大重试次数，默认 2")
    args = parser.parse_args()
    global args_namespace
    args_namespace = args

    health = api("GET", "/settings/llm-health", timeout=30)["data"]
    print("=" * 72)
    print("NovelCraft 10万字长篇验证")
    print(f"LLM configured: {health.get('configured')}")
    print(f"Provider/Model: {health.get('provider')} / {health.get('model')}")
    print(f"Reachable: {health.get('ok')}")
    if not health.get("ok"):
      print(f"Health error: {health.get('error')}")
      print("提示：当前模型不可达，继续运行通常只会退回 fallback。")
      if args.require_real_llm:
          print("当前启用了 --require-real-llm，因此脚本立即失败。")
          return 2

    project_id = ""
    job_id = ""
    try:
        project_id = create_project()
        print(f"\n[1/4] 已创建项目: {project_id}")

        attach_outline(project_id)
        print("[2/4] 已生成并挂载 24 章大纲")

        job_id = start_job(project_id)
        print(f"[3/4] 已启动长篇任务: {job_id}")

        print("\n[4/4] 轮询任务进度")
        started = time.time()
        while True:
            time.sleep(args.poll_seconds)
            job = api("GET", f"/ai/{job_id}", timeout=30)["data"]
            project = api("GET", f"/projects/{project_id}", timeout=30)["data"]
            elapsed = int(time.time() - started)
            print(
                f"  t+{elapsed:>4}s | status={job['status']:<10} "
                f"| completed={job['completed_chapters']:>2}/{job['total_chapters']:<2} "
                f"| fallback={job.get('fallback_chapters', 0):>2} "
                f"| words={job['generated_words']:>6} "
                f"| provider={job.get('llm_provider') or '-'} "
                f"| source={job.get('last_generation_source') or '-'} "
                f"| projectWords={project['currentWordCount']:>6}"
            )

            if job["status"] in {"completed", "failed", "cancelled"}:
                print("\n任务已结束。")
                if job.get("fallback_chapters", 0) > 0:
                    print("结论：存在 fallback 章节，本轮不能视为“平台上的真实 AI 完成长篇写作”。")
                    if args.require_real_llm:
                        print("当前启用了 --require-real-llm，因此脚本返回失败。")
                        return 3
                elif project["currentWordCount"] >= 100000:
                    print("结论：已达到 10 万字门槛，且未发现 fallback 章节。")
                else:
                    print("结论：任务结束，但总字数尚未达到 10 万。需要继续优化调度或章节目标。")
                break

            if elapsed >= args.wait_seconds:
                print("\n达到等待上限，先停止轮询。")
                print("建议根据上面的 completed / fallback / words 指标继续判断。")
                break

        return 0
    except HTTPError as exc:
        print(f"HTTPError: {exc}")
        return 1
    except URLError as exc:
        print(f"URLError: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}")
        return 1
    finally:
        if job_id and not args.keep_project:
            cancel_job(job_id)
        if project_id and not args.keep_project:
            delete_project(project_id)


if __name__ == "__main__":
    sys.exit(main())
