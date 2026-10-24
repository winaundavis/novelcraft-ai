"""长篇创作任务 — 10 万字全程监控报告脚本

用法:
  python scripts/long_run_report.py [--chapters N] [--words N] [--interval S]

默认: 10 章 / 10000 字 / 3 秒轮询

产出:
  - test_artifacts/long_run_report.csv    逐次轮询数据
  - test_artifacts/long_run_report.md     可读摘要报告
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

API_BASE = os.environ.get("NOVELCRAFT_API_BASE", "http://127.0.0.1:8000")
ARTIFACTS_DIR = os.path.join(os.path.dirname(__file__), "..", "test_artifacts")
CSV_PATH = os.path.join(ARTIFACTS_DIR, "long_run_report.csv")
MD_PATH = os.path.join(ARTIFACTS_DIR, "long_run_report.md")

# ── 配置 ──────────────────────────────────────────────────
NUM_CHAPTERS = 6
TARGET_WORDS_PER_CHAPTER = 1600
POLL_INTERVAL_S = 3
JOB_TIMEOUT_MINUTES = 30

# ── HTTP 工具 ─────────────────────────────────────────────
def _api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"API 请求失败 {method} {path}: {exc}") from exc


# ── 主流程 ─────────────────────────────────────────────────
def main():
    chapters = int(sys.argv[1]) if len(sys.argv) > 1 else NUM_CHAPTERS
    words_per_chapter = int(sys.argv[2]) if len(sys.argv) > 2 else TARGET_WORDS_PER_CHAPTER
    poll_s = int(sys.argv[3]) if len(sys.argv) > 3 else POLL_INTERVAL_S

    total_target = chapters * words_per_chapter
    print(f"🚀 长篇创作监控报告")
    print(f"   章节数: {chapters}  目标/章: {words_per_chapter}字  总目标: ~{total_target}字")
    print(f"   轮询间隔: {poll_s}s  超时: {JOB_TIMEOUT_MINUTES}min")
    print()

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    start_time = time.time()
    project_id = None
    job_id = None
    report_rows: list[dict] = []

    try:
        # ── 1. 创建项目 ──────────────────────────────
        print("1/5 创建项目...")
        proj = _api("POST", "/api/projects/", {
            "name": f"长跑测试-{int(time.time())}",
            "subtitle": "长篇创作监控报告自动测试",
            "genre": "scifi",
            "targetWordCount": total_target,
            "premise": "一名星际档案修复师在废弃空间站中发现了一份被篡改的人类文明史，她必须在真相与谎言之间做出抉择。",
            "targetChapters": chapters,
            "stylePreset": "epic",
        })
        project_id = proj["data"]["id"]
        print(f"   ✓ 项目 {project_id} 已创建")

        # ── 2. 生成大纲 ──────────────────────────────
        print("2/5 生成大纲...")
        outline = _api("POST", "/api/ai/generate-outline", {
            "premise": "一名星际档案修复师在废弃空间站中发现了一份被篡改的人类文明史，她必须在真相与谎言之间做出抉择。",
            "genre": "scifi",
            "targetWordCount": total_target,
            "targetChapters": chapters,
            "structure": "three_act",
            "additionalNotes": "每章都要有明确冲突、信息揭示和悬念推进。"
        })
        _api("PATCH", f"/api/projects/{project_id}", {
            "outline": outline["data"],
            "targetChapters": chapters,
        })
        print(f"   ✓ 大纲已生成并绑定到项目")

        # ── 3. 启动长篇任务 ──────────────────────────
        print("3/5 启动长篇创作任务...")
        job_start = _api("POST", "/api/ai/novel-jobs/start", {
            "projectId": project_id,
            "mode": "all",
        })
        job_id = job_start["data"]["id"]
        print(f"   ✓ 任务 {job_id} 已启动 (mode=all, {chapters}章)")

        # ── 4. 轮询监控 ──────────────────────────────
        print(f"4/5 开始轮询（每 {poll_s}s）...")
        print(f"   {'时间':<20s} {'状态':<12s} {'完成':>5s} {'失败':>4s} {'当前章':<18s} {'累计字':>8s}")
        print(f"   {'─'*20} {'─'*12} {'─'*5} {'─'*4} {'─'*18} {'─'*8}")

        deadline = start_time + JOB_TIMEOUT_MINUTES * 60
        last_completed = 0
        retry_count = 0

        while time.time() < deadline:
            elapsed = int(time.time() - start_time)
            try:
                resp = _api("GET", f"/api/ai/novel-jobs/{job_id}")
                job = resp["data"]
            except Exception:
                retry_count += 1
                time.sleep(poll_s)
                continue

            status = job["status"]
            completed = job["completed_chapters"]
            failed = job["failed_chapters"]
            words = job["generated_words"]
            current_title = job.get("current_chapter_title") or ""
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # 失败重试检测
            if failed > 0 and completed == last_completed:
                retry_count += 1
            last_completed = completed

            print(f"   {now_iso[:19]:<20s} {status:<12s} {completed:>4d}/{chapters} {failed:>4d} {current_title[:18]:<18s} {words:>8,d}")

            report_rows.append({
                "timestamp": now_iso,
                "elapsed_s": elapsed,
                "status": status,
                "completed_chapters": completed,
                "failed_chapters": failed,
                "total_chapters": job["total_chapters"],
                "generated_words": words,
                "target_words": job["target_words"],
                "current_chapter_title": current_title,
                "message": job.get("message", ""),
                "error": job.get("error", ""),
                "retry_count": retry_count,
            })

            if status in ("completed", "failed", "cancelled"):
                break

            time.sleep(poll_s)

        # ── 5. 生成报告 ──────────────────────────────
        print(f"\n5/5 生成报告...")
        total_elapsed = int(time.time() - start_time)
        final_job = report_rows[-1] if report_rows else {}
        final_status = final_job.get("status", "unknown")
        final_words = final_job.get("generated_words", 0)
        final_completed = final_job.get("completed_chapters", 0)
        final_failed = final_job.get("failed_chapters", 0)

        # ── CSV ──────────────────────────────────────
        with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            if report_rows:
                writer = csv.DictWriter(f, fieldnames=list(report_rows[0].keys()))
                writer.writeheader()
                writer.writerows(report_rows)

        # ── Markdown 报告 ────────────────────────────
        with open(MD_PATH, "w", encoding="utf-8") as f:
            f.write(f"# 长篇创作监控报告\n\n")
            f.write(f"- **任务ID**: {job_id}\n")
            f.write(f"- **项目ID**: {project_id}\n")
            f.write(f"- **开始时间**: {report_rows[0]['timestamp'] if report_rows else 'N/A'}\n")
            f.write(f"- **结束时间**: {report_rows[-1]['timestamp'] if report_rows else 'N/A'}\n")
            f.write(f"- **总耗时**: {total_elapsed // 60}分{total_elapsed % 60}秒\n")
            f.write(f"- **轮询次数**: {len(report_rows)}\n")
            f.write(f"- **最终状态**: {final_status}\n")
            f.write(f"- **完成章节**: {final_completed}/{chapters}\n")
            f.write(f"- **失败章节**: {final_failed}\n")
            f.write(f"- **累计字数**: {final_words:,}\n")
            f.write(f"- **目标字数**: {total_target:,}\n")
            f.write(f"- **API 重试**: {retry_count}次\n\n")

            f.write("## 逐次轮询记录\n\n")
            f.write("| # | 时间 | 状态 | 完成 | 失败 | 当前章 | 累计字 | API重试 |\n")
            f.write("|---|------|------|------|------|--------|--------|----------|\n")
            for i, row in enumerate(report_rows):
                f.write(
                    f"| {i+1} | {row['timestamp'][:19]} | {row['status']} | "
                    f"{row['completed_chapters']} | {row['failed_chapters']} | "
                    f"{row['current_chapter_title'][:14]} | {row['generated_words']:,} | "
                    f"{row['retry_count']} |\n"
                )

            f.write(f"\n## 判定\n\n")
            if final_status == "completed" and final_failed == 0:
                f.write("✅ **全部成功** — 长篇创作任务顺利完成。\n")
            elif final_status == "completed":
                f.write(f"⚠️ **部分成功** — 完成 {final_completed} 章，失败 {final_failed} 章。\n")
            elif final_status == "failed":
                f.write(f"❌ **任务失败** — {final_job.get('error', '未知错误')}\n")
            elif final_status == "running":
                f.write(f"⏳ **任务仍在运行**（超时截断）— 已完成 {final_completed} 章\n")
            else:
                f.write(f"ℹ️ **状态**: {final_status}\n")

        print(f"   ✓ CSV  → {CSV_PATH}")
        print(f"   ✓ 报告 → {MD_PATH}")
        print(f"\n{'='*50}")
        print(f"📊 最终: {final_status} | {final_completed}/{chapters}章 | {final_words:,}字 | 耗时{total_elapsed}s")
        print(f"{'='*50}")

    except Exception as exc:
        print(f"\n❌ 异常中断: {exc}")
        raise
    finally:
        # 清理
        if job_id:
            try:
                _api("POST", f"/api/ai/novel-jobs/{job_id}/cancel")
            except Exception:
                pass
        if project_id:
            try:
                _api("DELETE", f"/api/projects/{project_id}")
                print(f"   ✓ 已清理测试项目")
            except Exception:
                pass


if __name__ == "__main__":
    main()
