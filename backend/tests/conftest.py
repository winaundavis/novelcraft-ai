"""pytest 配置：每个测试进程只写隔离的临时数据目录，默认 0 次真实 LLM 调用。"""
import sys
import os
import httpx
import pytest

# 确保 backend 目录在 import 路径中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
# 同目录助手（pytest prepend 导入模式会把 tests/ 加入 sys.path）
sys.path.insert(0, os.path.dirname(__file__))

from isolation import activate_temp_environment, assert_app_isolated, temp_root  # noqa: E402


def pytest_configure(config):
    """在导入 app 之前建立临时环境，然后校验解析后的路径确实落在临时目录内。"""
    activate_temp_environment(prefix="novelcraft-pytest-")
    from app import db
    db.init_db()
    # 导入 app 后立即校验：DB / data / uploads 都必须位于临时根目录内。
    # 若 backend/.env 的真实 NOVELCRAFT_DB_PATH 生效，这里会立刻失败而不是静默写真实库。
    assert_app_isolated()


class _BlockedAsyncClient:
    """Default-suite circuit breaker: tests never make a real HTTP model call."""
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        type(self).calls += 1
        raise httpx.NetworkError("live LLM calls are disabled in the default pytest suite")

    async def __aexit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _disable_live_llm_by_default(monkeypatch):
    # Legacy endpoint tests emulate the explicit local-development mode. Production
    # security tests override this to exercise invite-only behavior.
    from app.core.config import settings
    monkeypatch.setattr(settings, "debug", True)
    if os.getenv("RUN_LIVE_LLM") != "1":
        monkeypatch.setattr("app.llm.provider.httpx.AsyncClient", _BlockedAsyncClient)


@pytest.fixture(autouse=True)
def _reset_runtime_rate_limiters():
    """Keep the in-process limiter isolated between tests.

    Production intentionally keeps these buckets process-local for the
    single-instance deployment.  A full pytest run must not let a prior test's
    synthetic requests change the status of a later, unrelated test.
    """
    from main import _auth_windows, _register_windows, _request_windows

    for bucket in (_auth_windows, _register_windows, _request_windows):
        bucket.clear()
    yield
    for bucket in (_auth_windows, _register_windows, _request_windows):
        bucket.clear()


@pytest.fixture
def live_llm_call_spy():
    """暴露被拦截的真实 provider HTTP 调用计数（默认应为 0）。"""
    before = _BlockedAsyncClient.calls
    yield lambda: _BlockedAsyncClient.calls - before


@pytest.fixture(scope="session")
def isolated_root():
    """当前 pytest 会话的临时数据根目录。"""
    return temp_root()


# ---------------------------------------------------------------------------
# 部分开源边界：以下用例断言的是**提示词正文**或**质量策略的具体实现**，
# 这些实现不随本仓库发布（`*_PROMPT` 常量已替换为占位内容）。
# 因此显式跳过，而不是删除——它们记录了对应的契约行为，便于自行实现后复用。
# ---------------------------------------------------------------------------
_STRATEGY_DEPENDENT = {
    "tests/test_fact_check_service.py::test_loop_reconcilable_records_extra_constraints_then_passes",
    "tests/test_fulltext_service.py::TestAskFulltext::test_asks_with_stable_prefix",
    "tests/test_outline_chunked_generation.py::test_truncated_outline_is_regenerated_per_volume",
    "tests/test_phase7a_experiment_preparation.py::test_prepare_uses_isolated_snapshot_and_freezes_real_harness",
    "tests/test_phase7a_packaging.py::test_source_archive_has_posix_entries_and_extracts_key_paths",
    "tests/test_review_service.py::test_ngram_rewrite_long_fragment_uses_fragment_prompt",
    "tests/test_rewrite_version_replace.py::test_rewrite_change_setting_supersedes_old_facts",
    "tests/test_rewrite_version_replace.py::test_rewrite_delete_setting_supersedes_old_fact",
    "tests/test_rewrite_version_replace.py::test_rewrite_revert_setting_reactivates_fact",
    "tests/test_rewrite_version_replace.py::test_rewrite_unchanged_setting_keeps_old_facts",
    "tests/test_world_rule_injection.py::test_continue_system_prompt_declares_rules_as_hard_constraint",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid in _STRATEGY_DEPENDENT:
            item.add_marker(pytest.mark.skip(reason="断言提示词/策略实现，该实现未随本仓库开源"))

