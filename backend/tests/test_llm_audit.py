# -*- coding: utf-8 -*-
"""LLM 调用审计（Codex 六轮 P1）单元测试。

验证：
- record_llm_call 成功写入审计表并返回 id
- source / status / duration / output_chars 落库
- project_id / job_id / role 从审计上下文（contextvar）自动带出
- 通过 projectId / jobId / role / since 过滤查询
- 表缺失时静默失败（不阻塞主流程）
"""
import sqlite3
import time

import pytest

from app.llm import audit as audit_mod
from app.llm.audit import (
    clear_llm_audit_context,
    query_llm_audit,
    record_llm_call,
    record_llm_postprocess,
    set_llm_audit_context,
)


@pytest.fixture(autouse=True)
def _clean_audit_ctx():
    clear_llm_audit_context()
    yield
    clear_llm_audit_context()


@pytest.fixture()
def audit_db(tmp_path, monkeypatch):
    """把审计写入指向临时库，测试不污染真实 DB。"""
    db_path = tmp_path / "audit_test.db"
    monkeypatch.setattr(audit_mod, "DB_PATH", db_path)
    return db_path


def _drop_table(db_path):
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE IF EXISTS llm_audit_log")
        con.commit()
    finally:
        con.close()


def test_record_success_writes_row(audit_db):
    audit_id = record_llm_call(
        provider="deepseek", model="deepseek-chat",
        source="llm", status="success", duration_ms=1200, output_chars=500,
    )
    assert audit_id
    rows = query_llm_audit(minutes=10, scope="all")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == audit_id
    assert row["provider"] == "deepseek"
    assert row["model"] == "deepseek-chat"
    assert row["source"] == "llm"
    assert row["status"] == "success"
    assert row["duration_ms"] == 1200
    assert row["output_chars"] == 500


def test_outline_postprocess_diagnostics_update_existing_audit_without_content(audit_db):
    audit_id = record_llm_call(
        provider="arts", model="deepseek-v4-flash", source="llm", status="success",
    )

    updated = record_llm_postprocess(
        audit_id,
        status="failed",
        stage="validate",
        code="node_reference_invalid",
        response_shape="nested_volumes",
        parsed_node_count=12,
    )

    assert updated is True
    row = query_llm_audit(minutes=10, scope="all")[0]
    assert row["postprocess_status"] == "failed"
    assert row["postprocess_stage"] == "validate"
    assert row["postprocess_code"] == "node_reference_invalid"
    assert row["response_shape"] == "nested_volumes"
    assert row["parsed_node_count"] == 12
    assert "prompt" not in row
    assert "response_body" not in row


def test_record_timeout_and_error(audit_db):
    record_llm_call(provider="kimi", model="kimi-k2.6", source="llm", status="timeout", duration_ms=90000)
    record_llm_call(provider="kimi", model="kimi-k2.6", source="llm", status="error", duration_ms=300)
    record_llm_call(provider="kimi", model="kimi-k2.6", source="fallback", status="error")
    rows = query_llm_audit(minutes=10, scope="all")
    assert len(rows) == 3
    statuses = {r["status"] for r in rows}
    assert statuses == {"timeout", "error"}
    sources = {r["source"] for r in rows}
    assert sources == {"llm", "fallback"}


def test_audit_context_auto_binds_project_job_role(audit_db):
    set_llm_audit_context(project_id="proj-a", job_id="job-1", role="writing")
    record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", duration_ms=10)
    rows = query_llm_audit(minutes=10, scope="all")
    assert len(rows) == 1
    assert rows[0]["project_id"] == "proj-a"
    assert rows[0]["job_id"] == "job-1"
    assert rows[0]["role"] == "writing"


def test_budget_audit_has_chapter_operation_dimension_and_unknown_actual_tokens(audit_db):
    set_llm_audit_context(
        project_id="proj-a", job_id="job-1", chapter_id="chapter-3",
        operation="fact_check", budget_dimension="qa", budget_before=0, budget_after=1,
    )
    record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", est_tokens=800)
    row = query_llm_audit(minutes=10, scope="all")[0]
    assert (row["chapter_id"], row["operation"], row["budget_dimension"]) == ("chapter-3", "fact_check", "qa")
    assert (row["budget_before"], row["budget_after"], row["actual_tokens"]) == (0, 1, None)


def test_audit_context_cleared(audit_db):
    set_llm_audit_context(project_id="proj-a", job_id="job-1")
    clear_llm_audit_context()
    record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", duration_ms=10)
    rows = query_llm_audit(minutes=10, scope="all")
    assert rows[0]["project_id"] == ""
    assert rows[0]["job_id"] == ""


def test_explicit_project_overrides_context(audit_db):
    set_llm_audit_context(project_id="ctx-proj", job_id="ctx-job")
    record_llm_call(
        provider="deepseek", model="deepseek-chat",
        source="llm", status="success", duration_ms=10,
        project_id="explicit-proj", job_id="explicit-job",
    )
    rows = query_llm_audit(minutes=10, scope="all")
    assert rows[0]["project_id"] == "explicit-proj"
    assert rows[0]["job_id"] == "explicit-job"


def test_query_filter_by_project_and_job(audit_db):
    set_llm_audit_context(project_id="proj-a", job_id="job-1")
    record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", duration_ms=10)
    set_llm_audit_context(project_id="proj-b", job_id="job-2")
    record_llm_call(provider="kimi", model="kimi-k2.6", source="llm", status="success", duration_ms=20)

    rows = query_llm_audit(minutes=10, scope="all", project_id="proj-a")
    assert len(rows) == 1
    assert rows[0]["job_id"] == "job-1"

    rows = query_llm_audit(minutes=10, scope="all", job_id="job-2")
    assert len(rows) == 1
    assert rows[0]["provider"] == "kimi"


def test_query_order_desc(audit_db):
    for i in range(3):
        record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", duration_ms=i)
        time.sleep(0.01)
    rows = query_llm_audit(minutes=10, scope="all")
    assert len(rows) == 3
    durations = [r["duration_ms"] for r in rows]
    assert durations == sorted(durations, reverse=True)


def test_since_filter(audit_db):
    record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", duration_ms=1)
    time.sleep(0.02)
    # 用第一条记录的真实 created_at（含微秒）作为 since，第二条必然晚于它
    first_created = query_llm_audit(minutes=10, scope="all")[0]["created_at"]
    record_llm_call(provider="kimi", model="kimi-k2.6", source="llm", status="success", duration_ms=2)
    rows = query_llm_audit(since=first_created, scope="all")
    assert len(rows) == 2
    rows = query_llm_audit(since=first_created, scope="all", project_id="__none__")
    assert len(rows) == 0


def test_missing_table_silent(audit_db):
    """表不存在时 record/query 静默失败，不抛异常。"""
    _drop_table(audit_db)
    audit_id = record_llm_call(provider="deepseek", model="deepseek-chat", source="llm", status="success", duration_ms=1)
    # 表缺失 → record 自动建表后成功写入（幂等建表兜底）
    assert audit_id
    rows = query_llm_audit(minutes=10, scope="all")
    assert len(rows) == 1


# ═══════════════════════════════════════════════════════════
# T5：默认审计查询限定当前 run
# ═══════════════════════════════════════════════════════════

from app.services import run_context  # noqa: E402


def _record_run(run_id: str, *, project_id: str = "", job_id: str = "", role: str = "", status: str = "success") -> str:
    token = run_context._RUN_ID.set(run_id)
    try:
        return record_llm_call(
            provider="deepseek", model="deepseek-v4-flash", source="llm", status=status,
            project_id=project_id, job_id=job_id, role=role, duration_ms=10,
        )
    finally:
        run_context.end_run(token)


def test_default_scope_returns_only_current_run(audit_db):
    """A/B 两个 run 的记录不得互相混入默认查询结果。"""
    _record_run("run-a", project_id="proj-a")
    _record_run("run-b", project_id="proj-b")

    token = run_context._RUN_ID.set("run-a")
    try:
        rows = query_llm_audit(minutes=10)  # 默认 scope="run"
    finally:
        run_context.end_run(token)

    assert [row["run_id"] for row in rows] == ["run-a"]
    assert rows[0]["run_scope"] == "current"
    assert rows[0]["project_id"] == "proj-a"


def test_no_run_context_returns_empty_not_full_table(audit_db):
    """没有任何 run 上下文时明确返回空，而不是隐式全表。"""
    _record_run("run-a")
    _record_run("run-b")
    clear_llm_audit_context()
    run_context.clear_run()

    assert query_llm_audit(minutes=10) == []
    assert len(query_llm_audit(minutes=10, scope="all")) == 2


def test_explicit_run_id_can_read_history(audit_db):
    """历史 run 通过显式 run_id 查询：该 run 即本次查询范围（current），其余为 other。"""
    _record_run("run-old", job_id="job-old")
    _record_run("run-new", job_id="job-new")

    token = run_context._RUN_ID.set("run-new")
    try:
        rows = query_llm_audit(run_id="run-old", minutes=10)
        # 显式命名全量范围时才能看到其他 run，并被标注为 other
        all_rows = query_llm_audit(scope="all", minutes=10)
    finally:
        run_context.end_run(token)

    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-old"
    assert rows[0]["run_scope"] == "current"
    by_run = {row["run_id"]: row["run_scope"] for row in all_rows}
    assert by_run["run-new"] == "current"
    assert by_run["run-old"] == "other"


def test_legacy_rows_without_run_are_marked(audit_db):
    """旧的无 run 关联记录（run_id 为空）标记为 legacy，不重写历史 ID。"""
    record_llm_call(provider="deepseek", model="old", source="llm", status="success", duration_ms=5, output_chars=10)
    con = sqlite3.connect(audit_db)
    try:
        con.execute("UPDATE llm_audit_log SET run_id = ''")
        con.commit()
    finally:
        con.close()

    rows = query_llm_audit(minutes=10, scope="all")
    assert len(rows) == 1
    assert rows[0]["run_scope"] == "legacy"
    assert rows[0]["run_id"] == ""


def test_filters_compose_across_time_project_job_role_and_run(audit_db):
    """时间 / 项目 / job / role / run 过滤同时生效。"""
    _record_run("run-a", project_id="proj-a", job_id="job-1", role="writing")
    _record_run("run-a", project_id="proj-a", job_id="job-2", role="check")
    _record_run("run-b", project_id="proj-a", job_id="job-1", role="writing")

    token = run_context._RUN_ID.set("run-a")
    try:
        rows = query_llm_audit(minutes=10, project_id="proj-a", job_id="job-1", role="writing")
    finally:
        run_context.end_run(token)

    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-a"
    assert rows[0]["job_id"] == "job-1"
    assert rows[0]["project_id"] == "proj-a"


def test_failed_calls_are_scoped_and_reported(audit_db):
    """失败调用同样按 run 隔离，并保留 status 供诊断。"""
    _record_run("run-a", status="timeout")
    _record_run("run-b", status="error")

    token = run_context._RUN_ID.set("run-a")
    try:
        rows = query_llm_audit(minutes=10)
    finally:
        run_context.end_run(token)

    assert [(row["run_id"], row["status"]) for row in rows] == [("run-a", "timeout")]


def test_cache_hit_tokens_round_trips(audit_db):
    """provider 真实回传的缓存命中量必须落库并可查询。"""
    record_llm_call(
        provider="deepseek", model="deepseek-v4-flash", source="llm", status="success",
        prompt_tokens=1000, completion_tokens=20, total_tokens=1020, cache_hit_tokens=960,
    )
    row = query_llm_audit(minutes=10, scope="all")[0]
    assert row["cache_hit_tokens"] == 960
    assert row["actual_tokens"] == 1020


def test_cache_hit_tokens_null_when_provider_omits_it(audit_db):
    """provider 未回传缓存命中量时存 NULL（未知），不得伪造成 0。"""
    record_llm_call(provider="arts", model="deepseek-v4-flash", source="llm", status="success")
    row = query_llm_audit(minutes=10, scope="all")[0]
    assert row["cache_hit_tokens"] is None


def test_llm_audit_api_scopes_to_request_run(audit_db, monkeypatch):
    """端点级：请求级 run 传播 + runId/scope 语义 + 维护者权限。"""
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from main import app

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "invite_tokens", "maint-token:maint-user")
    monkeypatch.setattr(settings, "admin_user_ids", "maint-user")
    headers = {"X-NovelCraft-Invite": "maint-token"}

    _record_run("api-run", project_id="proj-api")

    with TestClient(app) as client:
        # 显式 runId → 只返回该 run
        explicit = client.get("/api/ai/llm-audit", params={"runId": "api-run"}, headers=headers)
        assert explicit.status_code == 200
        assert [row["run_id"] for row in explicit.json()["data"]] == ["api-run"]

        # 默认（无 runId）→ 使用本次请求自己的 run，不含历史 run
        default = client.get("/api/ai/llm-audit", headers=headers)
        assert default.status_code == 200
        assert default.json()["data"] == []
        request_run = default.headers.get("X-NovelCraft-Run-Id")
        assert request_run and request_run != "api-run"

        # 请求头可显式指定 run 作用域
        echoed = client.get("/api/ai/llm-audit", headers={**headers, "X-NovelCraft-Run-Id": "api-run"})
        assert echoed.headers.get("X-NovelCraft-Run-Id") == "api-run"
        assert [row["run_id"] for row in echoed.json()["data"]] == ["api-run"]

        # 全量范围必须显式命名
        full = client.get("/api/ai/llm-audit", params={"scope": "all"}, headers=headers)
        assert any(row["run_id"] == "api-run" for row in full.json()["data"])

        # 非法 scope 明确拒绝
        assert client.get("/api/ai/llm-audit", params={"scope": "everything"}, headers=headers).status_code == 422

        # 非维护者被拒绝
        monkeypatch.setattr(settings, "admin_user_ids", "")
        assert client.get("/api/ai/llm-audit", headers=headers).status_code == 403


def test_run_scope_does_not_bypass_project_daily_budget(audit_db):
    """run 隔离只影响审计查询；项目日预算是项目＋日期维度，切新 run 不重置额度。"""
    from app.core.config import settings
    from app.db import get_conn
    from app.llm.audit import reset_llm_audit_context
    from app.llm.provider import LLMBudgetExceededError
    from app.services.project_budget_service import consume_project_budget

    original_call_limit = settings.project_daily_llm_call_limit
    original_token_limit = settings.project_daily_token_limit
    token = set_llm_audit_context(project_id="proj-budget", merge=False)
    try:
        settings.project_daily_llm_call_limit = 1
        settings.project_daily_token_limit = 1000000
        run_context._RUN_ID.set("run-1")
        consume_project_budget(1)  # 第一次占用当日额度

        # 切到新 run：额度不重置，应继续被拒绝
        run_context._RUN_ID.set("run-2")
        with pytest.raises(LLMBudgetExceededError):
            consume_project_budget(1)
    finally:
        settings.project_daily_llm_call_limit = original_call_limit
        settings.project_daily_token_limit = original_token_limit
        reset_llm_audit_context(token)
        run_context.clear_run()
        with get_conn() as conn:
            conn.execute("DELETE FROM project_llm_usage WHERE project_id = ?", ("proj-budget",))
