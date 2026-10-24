# -*- coding: utf-8 -*-
"""e2e_report.py 六轮 P1 / 七轮 P1 门槛逻辑单元测试（subprocess 方式，pytest 兼容）。

用合成 Playwright JSON 报告 + 临时 sqlite DB，通过 subprocess 运行 e2e_report.py：
- live-llm 用例必须有 llm-evidence 证据；生成证明类（level=success）必须 source=llm/status=success
- 生命周期类（level=invoked）仅需 source=llm（任意状态，如 error/timeout）
- 七轮 P1：success-required 用例只有 source=llm/status=error → 必须返回非零退出码
- completed=0 的 job（仅启动未生成正文）且无对应证据 → violation
- 生成类用例 job completed>=1 + source=llm + fallback=0 → 通过
- 正常全量报告（31 项、0 skip、每 live 用例有证据）→ 退出码 0
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "e2e_report.py"
PYTHON = sys.executable


def _build_report_json(tests: list[dict], live_llm: str = "1") -> dict:
    """构造 Playwright JSON 报告结构。tests: [{title, status, annotations}]"""
    specs = []
    for t in tests:
        specs.append({
            "title": t["title"],
            "tests": [{
                "status": t["status"],
                "projectName": "chromium",
                "annotations": t.get("annotations", []),
                "results": [],
            }],
        })
    return {
        "config": {"metadata": {"RUN_LIVE_LLM": live_llm}},
        "suites": [{"title": "root", "specs": specs, "suites": []}],
    }


def _job_ann(project_id: str, job_id: str) -> dict:
    return {"type": "novelcraft-job", "description": json.dumps({"projectId": project_id, "jobId": job_id})}


def _live_ann() -> dict:
    return {"type": "live-llm", "description": "true"}


def _level_ann(level: str) -> dict:
    return {"type": "llm-evidence-level", "description": level}


def _evidence_ann(rows: list[dict]) -> dict:
    return {"type": "llm-evidence", "description": json.dumps(rows)}


def _success_evidence() -> dict:
    return _evidence_ann([
        {"source": "llm", "status": "success", "provider": "kimi", "model": "kimi-k2.6", "jobId": ""},
    ])


def _setup_db(jobs: list[dict]) -> str:
    """写临时 DB，返回 DB 路径。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute(
        """CREATE TABLE novel_jobs (
            id TEXT PRIMARY KEY, project_id TEXT, status TEXT, llm_provider TEXT,
            llm_model TEXT, last_generation_source TEXT, fallback_chapters INTEGER,
            completed_chapters INTEGER, error TEXT)"""
    )
    for j in jobs:
        con.execute(
            """INSERT INTO novel_jobs
            (id, project_id, status, llm_provider, llm_model, last_generation_source,
             fallback_chapters, completed_chapters, error)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (j["id"], j["project_id"], j["status"], j["llm_provider"], j["llm_model"],
             j["last_generation_source"], j["fallback_chapters"], j["completed_chapters"], j["error"]),
        )
    con.commit()
    con.close()
    return tmp.name


def _run_report(tests: list[dict], db_path: str, live_llm: str = "1") -> tuple[int, str]:
    """subprocess 运行 e2e_report.py，返回 (exit_code, output)。"""
    fd, json_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_build_report_json(tests, live_llm=live_llm), f, ensure_ascii=False)
    env = dict(os.environ)
    env["NOVELCRAFT_DB"] = db_path
    proc = subprocess.run(
        [PYTHON, str(SCRIPT), json_path],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    os.unlink(json_path)
    return proc.returncode, proc.stdout + proc.stderr


def _baseline_tests(extra_anns: list[dict], title: str = "T1 live") -> list[dict]:
    """构造 31 项报告：1 个 live 用例 + 30 个普通用例（EXPECTED_TOTAL=31）。"""
    tests = [
        {"title": title, "status": "expected", "annotations": extra_anns},
    ]
    for i in range(30):
        tests.append({"title": f"T{i}", "status": "expected", "annotations": []})
    return tests


def test_live_llm_with_success_evidence_passes(tmp_path):
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "completed",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "llm", "fallback_chapters": 0,
        "completed_chapters": 3, "error": "",
    }]
    tests = _baseline_tests([_live_ann(), _success_evidence(), _job_ann("p1", "job1")])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 0, out
    finally:
        os.unlink(db)


def test_live_llm_without_evidence_fails(tmp_path):
    """live 用例无任何证据（job 未完成 + 无 llm-evidence）→ 失败。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "running",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "", "fallback_chapters": 0,
        "completed_chapters": 0, "error": "",
    }]
    tests = _baseline_tests([_live_ann(), _job_ann("p1", "job1")])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 1, "live 用例无证据必须失败"
        assert "无任何 LLM 审计证据" in out or "未证明模型调用" in out
    finally:
        os.unlink(db)


def test_job_started_but_no_content_with_timeout_llm_passes(tmp_path):
    """job 已启动但未生成正文（completed=0），审计有 source=llm（timeout），
    用例标记 level=invoked（生命周期类 cancel 后可能中断）→ 真实调用 → 通过。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "running",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "", "fallback_chapters": 0,
        "completed_chapters": 0, "error": "",
    }]
    tests = _baseline_tests([
        _live_ann(),
        _level_ann("invoked"),
        _evidence_ann([{"source": "llm", "status": "timeout", "provider": "kimi", "model": "kimi-k2.6", "jobId": "job1"}]),
        _job_ann("p1", "job1"),
    ])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 0, out
    finally:
        os.unlink(db)


def test_success_required_with_error_only_fails(tmp_path):
    """Codex 七轮 P1：success-required（默认 level）live 用例只有 source=llm/status=error →
    必须返回非零退出码（error-only 不能算生成成功）。"""
    tests = _baseline_tests([
        _live_ann(),
        _evidence_ann([{"source": "llm", "status": "error", "provider": "kimi", "model": "kimi-k2.6", "jobId": ""}]),
    ])
    db = _setup_db([])
    try:
        code, out = _run_report(tests, db)
        assert code == 1, "success-required 只有 error 证据必须失败"
        assert "无 llm/success" in out or "生成成功证明缺失" in out
    finally:
        os.unlink(db)


def test_invoked_level_with_error_only_passes(tmp_path):
    """Codex 七轮 P1：invoked-level live 用例（生命周期类）只有 source=llm/status=error →
    真实调用了模型，应通过（不要求生成成功）。带 completed=0 的 job（启动未生成正文）。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "cancelled",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "", "fallback_chapters": 0,
        "completed_chapters": 0, "error": "",
    }]
    tests = _baseline_tests([
        _live_ann(),
        _level_ann("invoked"),
        _evidence_ann([{"source": "llm", "status": "error", "provider": "kimi", "model": "kimi-k2.6", "jobId": "job1"}]),
        _job_ann("p1", "job1"),
    ])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 0, out
    finally:
        os.unlink(db)


def test_job_started_no_llm_evidence_fails(tmp_path):
    """job 已启动但未生成正文（completed=0）且无 source=llm 证据（仅 fallback）→
    未证明真实模型调用 → 失败。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "running",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "", "fallback_chapters": 0,
        "completed_chapters": 0, "error": "",
    }]
    tests = _baseline_tests([
        _live_ann(),
        _evidence_ann([{"source": "fallback", "status": "error", "provider": "", "model": "", "jobId": "job1"}]),
        _job_ann("p1", "job1"),
    ])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 1, "仅 fallback（无 source=llm）必须判定未证明模型调用"
        assert (
            "未证明模型调用" in out or "无 source=llm" in out or "无 llm/success" in out
        )
    finally:
        os.unlink(db)


def test_job_started_with_success_evidence_passes(tmp_path):
    """job 已启动但未生成正文（completed=0），但有成功 llm-evidence → 通过。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "running",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "", "fallback_chapters": 0,
        "completed_chapters": 0, "error": "",
    }]
    tests = _baseline_tests([_live_ann(), _success_evidence(), _job_ann("p1", "job1")])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 0, out
    finally:
        os.unlink(db)


def test_job_fallback_fails(tmp_path):
    """生成类 job 完成但 fallback>0 → violation。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "completed",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "llm", "fallback_chapters": 2,
        "completed_chapters": 3, "error": "",
    }]
    tests = _baseline_tests([_live_ann(), _success_evidence(), _job_ann("p1", "job1")])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 1, "fallback 生成必须失败"
        assert "fallback" in out
    finally:
        os.unlink(db)


def test_missing_job_fails(tmp_path):
    """job annotation 但 DB 找不到 job → violation。"""
    tests = _baseline_tests([_live_ann(), _success_evidence(), _job_ann("p1", "missing-job")])
    db = _setup_db([])
    try:
        code, out = _run_report(tests, db)
        assert code == 1
        assert "缺少 job 记录" in out
    finally:
        os.unlink(db)


def test_live_llm_off_fails(tmp_path):
    """RUN_LIVE_LLM != 1 → violation。"""
    jobs = [{
        "id": "job1", "project_id": "p1", "status": "completed",
        "llm_provider": "kimi", "llm_model": "kimi-k2.6",
        "last_generation_source": "llm", "fallback_chapters": 0,
        "completed_chapters": 3, "error": "",
    }]
    tests = _baseline_tests([_live_ann(), _success_evidence(), _job_ann("p1", "job1")])
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db, live_llm="0")
        assert code == 1
        assert "RUN_LIVE_LLM 未启用" in out
    finally:
        os.unlink(db)


def test_full_31_with_all_live_evidence_passes(tmp_path):
    """完整 31 项报告：13 个 live 全部有 llm 证据 + 18 个本地无要求 → 通过。

    模拟真实验收形态：5 个生成类 job 完成 source=llm，8 个非 job live 有 success 证据。
    """
    jobs = [
        {
            "id": f"job{i}", "project_id": f"p{i}", "status": "completed",
            "llm_provider": "kimi", "llm_model": "kimi-k2.6",
            "last_generation_source": "llm", "fallback_chapters": 0,
            "completed_chapters": 2, "error": "",
        }
        for i in range(5)
    ]
    tests = []
    # 13 个 live 用例：5 个 job 类（completed>=1, source=llm + llm-evidence）+ 8 个非 job（success evidence）
    for i in range(5):
        tests.append({
            "title": f"live-job-{i}", "status": "expected",
            "annotations": [
                _live_ann(), _job_ann(f"p{i}", f"job{i}"),
                _evidence_ann([{"source": "llm", "status": "success", "provider": "kimi",
                                "model": "kimi-k2.6", "jobId": f"job{i}", "projectId": f"p{i}"}]),
            ],
        })
    for i in range(8):
        tests.append({
            "title": f"live-nonjob-{i}", "status": "expected",
            "annotations": [_live_ann(), _success_evidence()],
        })
    # 18 个本地确定性用例（无 live 标记，无证据要求）
    for i in range(18):
        tests.append({"title": f"local-{i}", "status": "expected", "annotations": []})
    assert len(tests) == 31
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 0, out
        assert "31/31 passed" in out
        assert "live succeeded 13/13" in out
    finally:
        os.unlink(db)


def test_live_missing_evidence_fails_even_if_local_pass(tmp_path):
    """13 个 live 中 1 个无证据 → 即使 31 项都 passed 也验收失败。"""
    jobs = []
    tests = []
    for i in range(13):
        anns = [_live_ann()]
        if i != 12:  # 最后一个 live 无任何证据
            anns.append(_success_evidence())
        tests.append({"title": f"live-{i}", "status": "expected", "annotations": anns})
    for i in range(18):
        tests.append({"title": f"local-{i}", "status": "expected", "annotations": []})
    assert len(tests) == 31
    db = _setup_db(jobs)
    try:
        code, out = _run_report(tests, db)
        assert code == 1
        assert "无任何 LLM 审计证据" in out
    finally:
        os.unlink(db)
