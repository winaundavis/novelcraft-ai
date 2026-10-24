# -*- coding: utf-8 -*-
"""E2E 独立可核验报告生成器（Codex P2-4 / P1-2 / P2-1 / 六轮 P1）。

输入：Playwright JSON 报告路径（E2E_REPORT_FILE 或 playwright.config.ts 默认仓库外路径）
输出：仓库外 Markdown 审计报告（同目录 *_summary.md）+ 验收结论（退出码）

关键设计（应对 Codex 五轮 P1-2 / P2-1 + 六轮 P1）：
- RUN_LIVE_LLM 从**报告 metadata**（测试运行时写入）读取，而非报告生成时的环境变量；
- 生成类用例通过 `novelcraft-job` annotation 记录精确 jobId，报告**按 jobId 查 DB**，
  绝不查询"最近 N 条历史任务"（避免把历史长跑伪装成本次证据）；
- **六轮 P1**：每个标记 `live-llm` 的用例必须提供**真实 LLM 证据**：
    - 前端用例通过 `llm-evidence` annotation 记录本次触发的后端审计记录
      （provider / model / source(llm|fallback) / status / duration / job 关联）；
    - 生成类用例若 job `completed_chapters >= 1` 且 `last_generation_source == llm`、
      `fallback == 0`，也算真实生成证据；
    - **"job 已启动但未生成正文"（completed=0 且无成功 llm 审计）→ 标"未证明模型调用"，
      不能当通过证据**；
- 验收门槛（不满足返回非零退出码）：
  1. RUN_LIVE_LLM == "1"
  2. 用例总数 == EXPECTED_TOTAL
  3. skipped == 0、failed == 0
  4. **每个标记 live-llm 的用例 ≥1 条真实 LLM 证据**（llm-evidence 含 success，
     或 job 完成正文 source=llm）
  5. 生成类用例缺 job 记录 → 失败
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DB_PATH = os.environ.get("NOVELCRAFT_DB", r"<REPO_ROOT>\creat\novelcraft-ai\backend\data\novelcraft.db")
# 输出目录不能写死 Windows 绝对路径：在 Linux/macOS 上那会被当成**单个文件名**，
# 于是在当前目录里造出一个名为 "<REPO_ROOT>\creat-reports" 的垃圾目录，
# 连带让源码打包测试失败（归档里出现含反斜杠的条目）。
# 默认放到系统临时目录，可用 NOVELCRAFT_REPORT_DIR 覆盖。
DEFAULT_OUTPUT_DIR = os.environ.get(
    "NOVELCRAFT_REPORT_DIR",
    os.path.join(tempfile.gettempdir(), "novelcraft-e2e-reports"),
)
EXPECTED_TOTAL = 31  # 完整 E2E 全量运行总用例数（13 live + 18 本地确定性；Codex 七轮新增参考分析 fallback 本地用例）


def _iter_specs(suites):
    """递归遍历嵌套 suites，产出 spec。"""
    for suite in suites or []:
        for spec in suite.get("specs", []):
            yield spec
        yield from _iter_specs(suite.get("suites", []))


def load_json_report(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def load_jobs_by_ids(job_ids: list[str]) -> dict[str, dict]:
    """按精确 job id 查询 DB（Codex P1-2：不查询"最近 N 条"）。"""
    if not job_ids:
        return {}
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(job_ids))
    rows = con.execute(
        "SELECT id, project_id, status, llm_provider, llm_model, last_generation_source, "
        "fallback_chapters, completed_chapters, error "
        f"FROM novel_jobs WHERE id IN ({placeholders})",
        job_ids,
    ).fetchall()
    con.close()
    return {r["id"]: dict(r) for r in rows}


def escape_md(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _evidence_rows(annotations: list[dict]) -> list[dict]:
    """解析 llm-evidence annotation（前端写入的结构化审计记录 JSON 数组）。"""
    for ann in annotations:
        if ann.get("type") == "llm-evidence":
            try:
                rows = json.loads(ann.get("description") or "[]")
                if isinstance(rows, list):
                    return [r for r in rows if isinstance(r, dict)]
            except (ValueError, TypeError):
                return []
    return []


def _is_live_llm_test(t: dict) -> bool:
    """用例是否标记了 live-llm（前端 markLiveLlm() 写入）。"""
    return any(ann.get("type") == "live-llm" for ann in t.get("annotations", []))


def _evidence_has_success(rows: list[dict]) -> bool:
    """证据中是否含 ≥1 条 source=llm 且 status=success 的真实成功调用（生成证明）。"""
    return any(
        (r.get("source") or "") == "llm" and (r.get("status") or "") == "success"
        for r in rows
    )


def _evidence_has_any_llm(rows: list[dict]) -> bool:
    """证据中是否含 ≥1 条 source=llm 的记录（任意状态，证明真实调用发生）。

    Codex 七轮 P1：生命周期类用例（启动/SSE/取消）cancel 后 job 可能中断，
    产生 error/timeout 审计——这仍证明"真实调用了模型"，归类为「live invoked」；
    而 llm/success 才是「live succeeded」（生成成功证明）。报告分别统计两者。
    """
    return any((r.get("source") or "") == "llm" for r in rows)


def _evidence_level(t: dict) -> str:
    """读取用例声明的 LLM 证据要求等级（前端 markLlmRequirement 写入）：
    - 'invoked'：仅需真实调用发生（生命周期类：启动/SSE/取消）
    - 'success'（默认）：必须生成成功（source=llm 且 status=success）
    """
    for ann in t.get("annotations", []):
        if ann.get("type") == "llm-evidence-level":
            level = str(ann.get("description") or "success")
            if level in {"invoked", "success"}:
                return level
    return "success"


def _job_table_proof(t: dict, jobs: dict[str, dict], require_success: bool) -> bool:
    """按 annotation jobId 查 job 表：completed>=1 + source=llm（+ fallback=0）即真实生成证据。
    require_success=True 时额外要求 fallback_chapters==0（生成成功证明）。"""
    for ann in t.get("annotations", []):
        if ann.get("type") == "novelcraft-job":
            try:
                job_id = json.loads(ann.get("description") or "{}").get("jobId", "")
            except (ValueError, TypeError):
                continue
            job = jobs.get(job_id)
            if not job:
                continue
            if (job.get("completed_chapters", 0) or 0) >= 1 \
                    and (job.get("last_generation_source") or "").strip() == "llm":
                if not require_success or (job.get("fallback_chapters", 0) or 0) == 0:
                    return True
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python e2e_report.py <playwright-json-report>")
        return 2
    report_path = sys.argv[1]

    report = load_json_report(report_path)
    # 环境标记：从报告 metadata（测试运行时写入）读取（Codex P2-1）
    live_llm = str((report.get("config", {}).get("metadata") or {}).get("RUN_LIVE_LLM", "?"))

    tests = []
    for spec in _iter_specs(report.get("suites", [])):
        spec_title = spec.get("title", "")
        for test in spec.get("tests", []):
            raw_status = test.get("status", "unknown")
            if raw_status == "expected":
                status = "passed"
            elif raw_status == "unexpected":
                status = "failed"
            elif raw_status == "skipped":
                status = "skipped"
            else:
                status = raw_status
            duration = 0
            error = ""
            attachments = []
            annotations = test.get("annotations", [])
            for result in test.get("results", []):
                duration = max(duration, result.get("duration", 0) or 0)
                for err in result.get("errors", []):
                    if err.get("message"):
                        error = err["message"][:500]
                        break
                for att in result.get("attachments", []):
                    if att.get("path"):
                        attachments.append(att["path"])
            tests.append({
                "title": spec_title,
                "projectName": test.get("projectName", ""),
                "status": status,
                "duration_ms": duration,
                "error": error,
                "attachments": attachments,
                "annotations": annotations,
            })

    passed = sum(1 for t in tests if t["status"] == "passed")
    failed = sum(1 for t in tests if t["status"] == "failed")
    skipped = sum(1 for t in tests if t["status"] == "skipped")
    total = len(tests)

    # 按 annotation 收集本次运行的 jobId（Codex P1-2）
    job_ids = []
    job_annotations = {}  # jobId -> {"title": ..., "projectId": ...}
    for t in tests:
        for ann in t.get("annotations", []):
            if ann.get("type") == "novelcraft-job":
                try:
                    parsed = json.loads(ann.get("description") or "{}")
                    job_id = parsed.get("jobId", "")
                    if job_id:
                        job_ids.append(job_id)
                        job_annotations[job_id] = {
                            "title": t["title"],
                            "projectId": parsed.get("projectId", ""),
                        }
                except (ValueError, TypeError):
                    continue
    jobs = load_jobs_by_ids(job_ids)

    # 六轮 P1：收集每个用例的 LLM 证据（llm-evidence annotation）
    for t in tests:
        t["evidence"] = _evidence_rows(t.get("annotations", []))
        t["is_live"] = _is_live_llm_test(t)

    # Codex 七轮 P1：区分 live invoked（真实调用发生）与 live succeeded（生成成功）
    for t in tests:
        t["live_invoked"] = t["is_live"] and (
            _evidence_has_any_llm(t["evidence"]) or _job_table_proof(t, jobs, require_success=False)
        )
        t["live_succeeded"] = t["is_live"] and (
            _evidence_has_success(t["evidence"]) or _job_table_proof(t, jobs, require_success=True)
        )
    live_invoked = sum(1 for t in tests if t["live_invoked"])
    live_succeeded = sum(1 for t in tests if t["live_succeeded"])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append("# NovelCraft AI · E2E Live-LLM 验收报告（Codex P2-4 可核验 · 六轮 P1 真实模型证据）")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 报告来源：`{report_path}`")
    lines.append(f"- 环境标记：RUN_LIVE_LLM={live_llm}（0=未启用真实 LLM，1=已启用）")
    live_count_total = sum(1 for t in tests if t["is_live"])
    lines.append(f"- 用例统计：共 {total} 项｜通过 {passed}｜失败 {failed}｜跳过 {skipped}")
    lines.append(f"- 分类清单：{live_count_total} 项 @live-llm（需真实 LLM 来源证据）+ {total - live_count_total} 项本地确定性（不要求 source=llm）")
    lines.append(f"- live 证据：真实调用（invoked）{live_invoked}/{live_count_total} 项｜生成成功（succeeded）{live_succeeded}/{live_count_total} 项")
    lines.append(f"- 生成类用例 job 关联：{len(job_ids)} 个（按 annotation 精确 jobId）")
    lines.append(f"- LLM 审计证据：{sum(1 for t in tests if t['evidence'])} 个用例记录 llm-evidence")
    lines.append("")

    lines.append("## 一、逐项结果")
    lines.append("")
    lines.append("| # | 用例 | live | 状态 | 耗时 | 模型证据 | 备注/错误 |")
    lines.append("|---|------|------|------|------|----------|-----------|")
    for idx, t in enumerate(tests, 1):
        dur = f"{t['duration_ms'] / 1000:.1f}s" if t["duration_ms"] else "-"
        note = escape_md(t["error"][:120]) if t["error"] else ""
        status_icon = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}.get(t["status"], "❓")
        live_tag = "🟢 live" if t["is_live"] else ""
        evidence_summary = "—"
        if t["evidence"]:
            success_count = sum(1 for r in t["evidence"] if (r.get("source") == "llm" and r.get("status") == "success"))
            llm_count = sum(1 for r in t["evidence"] if r.get("source") == "llm")
            total_count = len(t["evidence"])
            evidence_summary = f"llm {llm_count}（成功 {success_count}）/ {total_count} 条"
        lines.append(f"| {idx} | {escape_md(t['title'])} | {live_tag} | {status_icon} {t['status']} | {dur} | {evidence_summary} | {note} |")
    lines.append("")

    lines.append("## 二、生成类用例模型来源（按 annotation jobId 精确关联）")
    lines.append("")
    if job_ids:
        lines.append("| 用例 | job | provider | model | source | fallback 章 | completed 章 | 结论 |")
        lines.append("|------|-----|----------|-------|--------|------------|--------------|------|")
        for job_id in job_ids:
            ann = job_annotations.get(job_id, {})
            job = jobs.get(job_id)
            if job:
                completed = job.get("completed_chapters", 0) or 0
                src = (job.get("last_generation_source") or "").strip()
                if completed >= 1 and src == "llm" and (job.get("fallback_chapters", 0) or 0) == 0:
                    conclusion = "✅ 真实 LLM 生成"
                elif completed >= 1 and src != "llm":
                    conclusion = "⚠️ 已生成但 source 非 llm"
                elif completed == 0:
                    conclusion = "⚠️ 仅启动未生成正文（未证明模型调用）"
                else:
                    conclusion = "⚠️ 存在 fallback"
                lines.append(
                    f"| {escape_md(ann.get('title', '-'))[:36]} | {job_id[:12]} "
                    f"| {escape_md(job['llm_provider']) or '-'} | {escape_md(job['llm_model']) or '-'} "
                    f"| {escape_md(src) or '-'} "
                    f"| {job['fallback_chapters']} | {completed} | {conclusion} |"
                )
            else:
                lines.append(f"| {escape_md(ann.get('title', '-'))[:36]} | {job_id[:12]} | ⚠️ 未找到 job 记录 | - | - | - | - | - |")
    else:
        lines.append("本次运行未捕获到生成类 job annotation（未跑生成类用例，或测试未记录 jobId）。")
    lines.append("")

    lines.append("## 三、live-LLM 用例真实模型证据明细（Codex 七轮 P1：分别统计 invoked / succeeded）")
    lines.append("")
    live_tests = [t for t in tests if t["is_live"]]
    if live_tests:
        lines.append("| 用例 | 证据条数 | 成功(llm/success) | provider/model | 调用(invoked) | 成功(succeeded) | 结论 |")
        lines.append("|------|----------|--------------------|----------------|---------------|-----------------|------|")
        for t in live_tests:
            ev = t["evidence"]
            success = [r for r in ev if r.get("source") == "llm" and r.get("status") == "success"]
            any_llm = [r for r in ev if r.get("source") == "llm"]
            provider_model = "—"
            if success:
                provider_model = f"{success[0].get('provider') or '-'}/{success[0].get('model') or '-'}"
            elif any_llm:
                provider_model = f"{any_llm[0].get('provider') or '-'}/{any_llm[0].get('model') or '-'}"
            invoked_mark = "✅" if t["live_invoked"] else "❌"
            succeeded_mark = "✅" if t["live_succeeded"] else "—"
            if t["live_succeeded"]:
                conclusion = "✅ 真实 LLM 生成证明（success）"
            elif t["live_invoked"]:
                conclusion = "⚠️ 仅调用证明（invoked，无 success——生命周期类可接受）"
            else:
                conclusion = "❌ 无真实 LLM 证据（仅 fallback/无）"
            lines.append(
                f"| {escape_md(t['title'])[:40]} | {len(ev)} | {len(success)} | {escape_md(provider_model)} "
                f"| {invoked_mark} | {succeeded_mark} | {conclusion} |"
            )
        lines.append("")
        lines.append("> 说明：真实 LLM 证据 = 后端 LLM 审计表（llm_audit_log）中存在 source=llm 记录。")
        lines.append("> invoked=真实调用了模型（source=llm 任意状态）；succeeded=生成成功（source=llm 且 status=success）。")
        lines.append("> 生命周期类用例（启动/SSE/取消）仅要求 invoked；生成证明类用例必须 succeeded。")
        lines.append("> fallback（降级占位）不算真实模型证据；'job 已启动但无 source=llm 记录' 标为未证明模型调用。")
        lines.append("")
    else:
        lines.append("本次运行没有标记为 live-llm 的用例。")
        lines.append("")

    lines.append("## 四、失败详情与产物")
    lines.append("")
    failed_tests = [t for t in tests if t["status"] == "failed"]
    if failed_tests:
        for idx, t in enumerate(failed_tests, 1):
            lines.append(f"### 失败 {idx}：{escape_md(t['title'])}")
            lines.append("")
            if t["error"]:
                lines.append("```")
                lines.append(t["error"][:800])
                lines.append("```")
                lines.append("")
            if t["attachments"]:
                lines.append("产物：")
                for att in t["attachments"]:
                    lines.append(f"- `{att}`")
                lines.append("")
    else:
        lines.append("无失败用例。")
        lines.append("")

    lines.append("## 五、清理结果")
    lines.append("")
    lines.append("- 用例 `deleteProject` 在 finally 中执行（发幂等 cancel → 直接 DELETE，后端强制清理）。")
    lines.append("- 若上表出现失败且涉及临时项目，项目可能残留，需人工确认。")
    lines.append("")

    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(
        DEFAULT_OUTPUT_DIR,
        os.path.basename(report_path).replace(".json", "_summary.md"),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"报告已生成：{out_path}")

    # ════════ 验收门槛（Codex P2-1 + 六轮 P1）════════
    violations = []
    if live_llm != "1":
        violations.append(f"RUN_LIVE_LLM 未启用（= {live_llm}）")
    if total != EXPECTED_TOTAL:
        violations.append(f"用例总数 {total} ≠ 期望 {EXPECTED_TOTAL}")
    if skipped > 0:
        violations.append(f"存在 {skipped} 个 skipped 用例")
    if failed > 0:
        violations.append(f"存在 {failed} 个 failed 用例")

    # 六轮 P1 / 七轮 P1：每个标记 live-llm 的用例必须有真实 LLM 证据，
    # 并按证据等级区分：'invoked' 仅需真实调用；'success'（默认）必须生成成功。
    # （仅当 RUN_LIVE_LLM=1 时要求；非 live 运行（RUN_LIVE_LLM=0）live 用例被 skip，
    #   不要求来源证据。非 live 用例（本地确定性）永不要求 source=llm）
    live_count = sum(1 for t in tests if t["is_live"])
    if live_llm == "1":
        for t in tests:
            if not t["is_live"]:
                continue
            title = t["title"]
            ev = t["evidence"]
            level = _evidence_level(t)
            # job 类 live 用例：job 表本身是证据（completed>=1 + source=llm + fallback=0）
            if _job_table_proof(t, jobs, require_success=True):
                continue  # job 表证明真实 LLM 生成成功
            if not ev:
                violations.append(f"live 用例「{title}」无任何 LLM 审计证据（未证明真实模型调用）")
                continue
            if level == "invoked":
                # 生命周期类（启动/SSE/取消）：仅要求真实调用了模型（source=llm 任意状态）
                if not _evidence_has_any_llm(ev):
                    violations.append(f"live 用例「{title}」无 source=llm 证据（仅 fallback 降级占位）")
            else:
                # 生成证明类：必须 llm/success（Codex 七轮 P1：error-only 不算成功）
                if not _evidence_has_success(ev):
                    violations.append(
                        f"live 用例「{title}」无 llm/success 证据（生成成功证明缺失，仅有调用或失败）"
                    )

    # 生成类用例（带 job annotation）精确校验（仅 live 运行要求）
    if live_llm == "1" and job_ids:
        for job_id in job_ids:
            job = jobs.get(job_id)
            ann = job_annotations.get(job_id, {})
            if not job:
                violations.append(f"生成类用例「{ann.get('title', job_id)}」缺少 job 记录（模型来源缺失）")
                continue
            completed = job.get("completed_chapters", 0) or 0
            if completed >= 1:
                # 真实完成了章节 → 必须证明是真实 LLM 且无 fallback
                src = (job.get("last_generation_source") or "").strip()
                if src != "llm":
                    violations.append(f"生成类用例「{ann.get('title', job_id)}」source={src or '(空)'}，非 llm")
                if job.get("fallback_chapters", 0) > 0:
                    violations.append(f"生成类用例「{ann.get('title', job_id)}」存在 {job['fallback_chapters']} 章 fallback")
                if not (job.get("llm_provider") and job.get("llm_model")):
                    violations.append(f"生成类用例「{ann.get('title', job_id)}」缺少 provider/model")
            else:
                # completed=0：job 已启动但未生成正文。Codex 七轮 P1：
                # 生命周期类用例（启动/SSE/取消，level=invoked）只需证明"真实调用了模型"——
                # 审计表存在 source=llm 记录（任意状态）即算真实调用；
                # 生成证明类用例（level=success）则必须 llm/success。
                owner = next(
                    (t for t in tests if any(
                        a.get("type") == "novelcraft-job"
                        and json.loads(a.get("description") or "{}").get("jobId") == job_id
                        for a in t.get("annotations", [])
                    )),
                    None,
                )
                ev = owner["evidence"] if owner else []
                level = _evidence_level(owner) if owner else "success"
                if level == "invoked":
                    if not _evidence_has_any_llm(ev):
                        violations.append(
                            f"生成类用例「{ann.get('title', job_id)}」job 已启动但未生成正文，"
                            "且无 source=llm 审计证据（未证明真实模型调用）"
                        )
                else:
                    if not _evidence_has_success(ev):
                        violations.append(
                            f"生成类用例「{ann.get('title', job_id)}」job 已启动但未生成正文，"
                            "且无 llm/success 证据（生成成功证明缺失）"
                        )
    elif live_llm == "1" and not job_ids:
        violations.append("本次运行无生成类 job 证据（不能证明真实 LLM 生成）")

    if violations:
        print("\n❌ 验收未通过：")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(
        f"\n✅ 验收通过：{total}/{EXPECTED_TOTAL} passed、0 skipped、RUN_LIVE_LLM=1、"
        f"live invoked {live_invoked}/{live_count}、live succeeded {live_succeeded}/{live_count}、"
        f"生成任务 source=llm、fallback=0。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

