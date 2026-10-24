"""测试隔离助手：保证任何测试进程只写临时数据目录。

导入顺序很关键：`app.db`、`app.llm.audit`、`main.py` 都在 **模块导入时**
解析数据库路径（`resolve_database_path(...)`）。因此必须在 `import app.*`
之前调用 :func:`activate_temp_environment`。

- pytest 由 `tests/conftest.py::pytest_configure` 自动调用本模块。
- 需要 `TestClient` 的独立脚本必须在导入 app 前自行调用，并在导入后调用
  :func:`assert_app_isolated` 校验解析结果落在临时根目录内。

不要复制真实库作为测试 fixture，也不要用清理生产行来掩盖测试误写。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_DB_ENV = "NOVELCRAFT_DB_PATH"
_DATA_ENV = "DATA_DIR"
_UPLOAD_ENV = "UPLOAD_DIR"
_ROOT_ENV = "NOVELCRAFT_TEST_ROOT"

_ATEXIT_REGISTERED: set[str] = set()


def activate_temp_environment(prefix: str = "novelcraft-test-") -> Path:
    """创建临时根目录并导出隔离环境变量。必须在导入 app 前调用。

    设置 DATA_DIR / NOVELCRAFT_DB_PATH / UPLOAD_DIR，并把 RUN_LIVE_LLM 默认
    设为 "0"（显式 shell 覆盖仍被尊重）。返回临时根目录（已 resolve）。
    """
    root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    os.environ[_DATA_ENV] = str(root / "data")
    os.environ[_UPLOAD_ENV] = str(root / "uploads")
    os.environ[_DB_ENV] = str(root / "data" / "novelcraft-test.db")
    # Override private .env values before Settings is imported. Synthetic keys
    # belong to individual tests; the default suite never inherits local ones.
    for name in (
        "APP_ENCRYPTION_KEY", "APP_ENCRYPTION_KEY_FILE", "INVITE_TOKENS",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY",
        "KIMI_API_KEY", "GLM_API_KEY", "ARTS_API_KEY",
    ):
        os.environ[name] = ""
    os.environ.setdefault("RUN_LIVE_LLM", "0")
    os.environ[_ROOT_ENV] = str(root)
    if str(root) not in _ATEXIT_REGISTERED:
        _ATEXIT_REGISTERED.add(str(root))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    return root


def temp_root() -> Path | None:
    """返回当前进程的临时根目录（未激活时为 None）。"""
    value = os.environ.get(_ROOT_ENV, "").strip()
    return Path(value).resolve() if value else None


def declared_paths() -> dict[str, Path]:
    """返回环境变量声明的隔离路径（尚未经过 app 解析）。"""
    return {
        "NOVELCRAFT_DB_PATH": Path(os.environ.get(_DB_ENV, "")).expanduser().resolve(),
        "DATA_DIR": Path(os.environ.get(_DATA_ENV, "")).expanduser().resolve(),
        "UPLOAD_DIR": Path(os.environ.get(_UPLOAD_ENV, "")).expanduser().resolve(),
    }


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate(checks: dict[str, Path], root: Path) -> dict[str, str]:
    offenders = {name: str(path) for name, path in checks.items() if not _is_within(path, root)}
    if offenders:
        detail = ", ".join(f"{name}={path}" for name, path in offenders.items())
        raise RuntimeError(
            "测试进程未隔离：以下路径不在临时根目录内 → "
            f"{detail}（临时根目录 {root}）。请在导入 app 之前设置 "
            "DATA_DIR / NOVELCRAFT_DB_PATH / UPLOAD_DIR。"
        )
    return {name: str(path) for name, path in checks.items()}


def assert_isolated(root: Path, *, db_path: Path | str, data_dir: Path | str | None = None,
                    upload_dir: Path | str | None = None) -> dict[str, str]:
    """校验显式给出的路径都位于临时根目录内；否则抛 RuntimeError。"""
    root = Path(root).resolve()
    checks = {"db_path": Path(db_path).expanduser().resolve()}
    if data_dir is not None:
        checks["data_dir"] = Path(data_dir).expanduser().resolve()
    if upload_dir is not None:
        checks["upload_dir"] = Path(upload_dir).expanduser().resolve()
    return _validate(checks, root)


def assert_app_isolated(root: Path | None = None) -> dict[str, str]:
    """校验已导入的 app 使用的 DB / 数据 / 上传路径都在临时根目录内。"""
    root = Path(root).resolve() if root is not None else temp_root()
    if root is None:
        raise RuntimeError("尚未激活临时环境（缺少 NOVELCRAFT_TEST_ROOT）。")
    from app import db as db_module
    from app.core.config import resolve_data_dir, settings
    from app.llm import audit as audit_module

    checks = {
        "app.db.DB_PATH": Path(db_module.DB_PATH).resolve(),
        "app.llm.audit.DB_PATH": Path(audit_module.DB_PATH).resolve(),
        "settings.data_dir": resolve_data_dir(settings.data_dir),
        "settings.upload_dir": Path(settings.upload_dir).expanduser().resolve(),
    }
    return _validate(checks, root)
