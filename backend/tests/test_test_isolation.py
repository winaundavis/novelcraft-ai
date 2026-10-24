"""T0：测试隔离回归——即便存在真实 backend/.env，测试也只写临时库。

这些用例本身不创建业务数据；它们验证隔离机制生效，并且在隔离失效时
会立刻失败（而不是静默写真实 Beta 库）。
"""
import asyncio
import os
from pathlib import Path

import httpx
import pytest

from isolation import (
    activate_temp_environment,
    assert_app_isolated,
    assert_isolated,
    declared_paths,
    temp_root,
)

# backend/.env 中的真实路径：../data/beta-user-001.db（相对 backend/）。
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_BETA_DB = (REPO_ROOT / "data" / "beta-user-001.db").resolve()


def test_session_temp_root_is_activated(isolated_root):
    assert isolated_root is not None
    assert isolated_root.exists()
    assert temp_root() == isolated_root


def test_declared_data_paths_live_under_temp_root(isolated_root):
    for name, path in declared_paths().items():
        assert path.is_relative_to(isolated_root), f"{name} 未隔离: {path}"


def test_app_resolved_paths_live_under_temp_root(isolated_root):
    resolved = assert_app_isolated(isolated_root)
    for name, path in resolved.items():
        assert Path(path).is_relative_to(isolated_root), f"{name} 未隔离: {path}"


def test_env_file_real_database_path_does_not_leak_into_tests():
    """backend/.env 的真实 DB 路径不得成为测试进程实际使用的库。"""
    from app import db as db_module
    from app.core.config import settings

    assert Path(db_module.DB_PATH).resolve() != REAL_BETA_DB
    assert not Path(db_module.DB_PATH).resolve().is_relative_to(REPO_ROOT / "data")
    # settings 即使加载了 .env，也应被进程环境变量覆盖为临时路径。
    assert Path(settings.data_dir).resolve().is_relative_to(temp_root())


def test_isolation_clears_inherited_secrets(monkeypatch):
    """Private local configuration must not enter default-suite objects or output."""
    from app.core.config import Settings

    names = [name.upper() for name in Settings.model_fields if name.endswith("_api_key")]
    names += ["APP_ENCRYPTION_KEY", "APP_ENCRYPTION_KEY_FILE", "INVITE_TOKENS"]
    with monkeypatch.context() as env:
        for name in names:
            env.setenv(name, "synthetic-inherited-value")
        for name in ("DATA_DIR", "NOVELCRAFT_DB_PATH", "UPLOAD_DIR", "NOVELCRAFT_TEST_ROOT"):
            env.setenv(name, os.environ.get(name, ""))
        activate_temp_environment(prefix="novelcraft-secret-isolation-")
        remaining = [name for name in names if os.environ.get(name)]
        assert remaining == []


def test_guard_rejects_path_outside_temp_root(isolated_root):
    outside = isolated_root.parent / "not-isolated.db"
    with pytest.raises(RuntimeError, match="测试进程未隔离"):
        assert_isolated(isolated_root, db_path=outside)


def test_guard_accepts_path_inside_temp_root(isolated_root):
    inside = isolated_root / "nested" / "ok.db"
    assert_isolated(isolated_root, db_path=inside, data_dir=isolated_root, upload_dir=isolated_root)


async def _enter_blocked_client(client):
    async with client:
        pass


def test_live_llm_transport_is_blocked_by_default():
    """默认套件必须把 provider 的 HTTP 传输层替换为熔断器（RUN_LIVE_LLM!=1）。"""
    import app.llm.provider as provider_module

    assert os.getenv("RUN_LIVE_LLM") != "1"
    assert provider_module.httpx.AsyncClient.__name__ == "_BlockedAsyncClient"


def test_blocked_transport_refuses_and_counts_live_calls(live_llm_call_spy):
    """真实调用尝试会被拒绝，并被 spy 记为 1 次（默认 0 次的证据）。"""
    import app.llm.provider as provider_module

    assert live_llm_call_spy() == 0
    with pytest.raises(httpx.NetworkError):
        asyncio.run(_enter_blocked_client(provider_module.httpx.AsyncClient()))
    assert live_llm_call_spy() == 1
