"""核心配置"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from typing import Optional
from pathlib import Path


_PRODUCTION_ENVIRONMENTS = {"production", "prod", "beta"}
_PLACEHOLDER_MARKERS = (
    "REPLACE_WITH",
    "EXAMPLE.INVALID",
    "YOUR_",
    "YOUR-",
    "CHANGE_ME",
    "CHANGEME",
    "<REPLACE",
    "LOCAL-DEV",
)


def resolve_data_dir(value: str) -> Path:
    return Path(value).expanduser().resolve()


def resolve_database_path(data_dir: Path, override: str) -> Path:
    return Path(override).expanduser().resolve() if override else data_dir / "novelcraft.db"


def resolve_cors_origins(value: str, *, debug: bool) -> list[str]:
    origins = [item.strip() for item in value.split(",") if item.strip()]
    if origins:
        return origins
    if debug:
        return ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"]
    return []


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    app_name: str = "NovelCraft AI"
    # Explicit environment mode. Production-like validation is opt-in so local
    # tests and development can still use the intentionally relaxed defaults.
    environment: str = "development"
    data_dir: str = "./data"
    novelcraft_db_path: str = ""
    debug: bool = False
    cors_allow_origins: str = ""
    allowed_hosts: str = ""
    upload_dir: str = "./uploads"
    upload_max_bytes: int = 10 * 1024 * 1024
    # Comma-separated ``invite-token:user_id`` pairs. Keep this server-only.
    invite_tokens: str = Field(default="", repr=False)
    admin_user_ids: str = "local-dev"
    project_daily_llm_call_limit: int = 30
    project_daily_token_limit: int = 120000
    # 非管理员每用户每日「按大纲自动创作」可用章数。
    # 与上面两项的成本护栏是**不同维度**：那两项按项目+日统计 LLM 调用/token，
    # 本项按用户+日统计成功落库的章数。<=0 在开发态表示不限制；生产环境校验
    # 会拒绝 <=0，避免误配置静默关闭产品层额度。
    user_daily_chapter_limit: int = 10
    # 灰度开关：小范围测试期可关闭产品层额度（关闭后非管理员也不受限）；
    # 正式上线前改回 true 即恢复。关闭时 user_daily_chapter_limit 的值保持不变，
    # 不需要重新计算或改数字。
    user_daily_chapter_limit_enabled: bool = True
    rate_limit: int = 30
    log_level: str = "INFO"
    health_path: str = "/api/health"
    # P2 伏笔账本：整书自动创作时是否每章自动追踪伏笔状态。
    # **默认关**：追踪走一次真实 LLM 调用并占用项目每日额度（每章 1 次）。
    # 想要自动维护伏笔状态时在 backend/.env 里置 FORESHADOW_AUTO_TRACK=true（需重启）。
    foreshadow_auto_track: bool = False
    auth_cookie_secure: bool = False
    # Fernet key used only for encrypting provider credentials stored in SQLite.
    # It must be injected out of band and never committed or stored in the DB.
    app_encryption_key: Optional[str] = Field(default=None, repr=False)
    app_encryption_key_file: Optional[str] = Field(default=None, repr=False)

    # 宽容解析 debug：允许 "true"/"false"/"1"/"0" 等字符串，非预期值默认 False
    @field_validator('debug', mode='before')
    @classmethod
    def parse_debug(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ('true', '1', 'yes', 'on')
        return False

    # 默认 LLM 配置
    default_llm_provider: str = "deepseek"
    default_llm_model: str = "deepseek-v4-flash"
    default_temperature: float = 0.8
    default_max_tokens: int = 4096

    # 建议2 全文问答：全书直灌截断保护（字数上限，环境变量 FULLTEXT_MAX_CHARS 可覆盖）
    fulltext_max_chars: int = 600000

    # API Keys（通过环境变量注入）
    openai_api_key: Optional[str] = Field(default=None, repr=False)
    openai_api_base: Optional[str] = None
    anthropic_api_key: Optional[str] = Field(default=None, repr=False)
    anthropic_api_base: Optional[str] = None
    deepseek_api_key: Optional[str] = Field(default=None, repr=False)
    qwen_api_key: Optional[str] = Field(default=None, repr=False)
    qwen_api_base: Optional[str] = None
    kimi_api_key: Optional[str] = Field(default=None, repr=False)
    kimi_api_base: Optional[str] = None
    glm_api_key: Optional[str] = Field(default=None, repr=False)
    glm_api_base: Optional[str] = None
    arts_api_key: Optional[str] = Field(default=None, repr=False)
    arts_api_base: Optional[str] = None


settings = Settings()


def is_production_environment(config: Settings | None = None) -> bool:
    """Return whether fail-closed production checks must apply."""
    current = config or settings
    return str(current.environment or "").strip().lower() in _PRODUCTION_ENVIRONMENTS


def contains_placeholder(value: str | None) -> bool:
    text = str(value or "").strip().upper()
    return bool(text) and any(marker in text for marker in _PLACEHOLDER_MARKERS)


def is_valid_encryption_key(value: str | None) -> bool:
    """Validate the Fernet key shape without exposing the key value."""
    raw = str(value or "").strip()
    if not raw or contains_placeholder(raw):
        return False
    try:
        from cryptography.fernet import Fernet

        Fernet(raw.encode("ascii"))
    except (ValueError, UnicodeEncodeError, TypeError):
        return False
    return True


def resolve_encryption_key(value: str | None, key_file: str | None = None) -> str:
    """Read an optional private key file; errors never include its path or contents."""
    direct = str(value or "").strip()
    location = str(key_file or "").strip()
    if location:
        try:
            from_file = Path(location).expanduser().read_text(encoding="utf-8").strip()
        except (OSError, ValueError, UnicodeError):
            raise ValueError("APP_ENCRYPTION_KEY_FILE cannot be read") from None
        if not from_file:
            raise ValueError("APP_ENCRYPTION_KEY_FILE is empty")
        if direct and direct != from_file:
            raise ValueError("APP_ENCRYPTION_KEY and APP_ENCRYPTION_KEY_FILE do not match")
        direct = from_file
    if direct and not is_valid_encryption_key(direct):
        raise ValueError("APP_ENCRYPTION_KEY is invalid")
    return direct


def production_config_errors(config: Settings | None = None) -> list[str]:
    """Return safe error codes for a production readiness gate.

    The function deliberately returns codes only. It never includes paths,
    credentials, invite values, or the contents of environment variables.
    """
    current = config or settings
    # A deployment that forgets ENVIRONMENT must not silently get the relaxed
    # development defaults while DEBUG is off. Local development can opt in by
    # explicitly setting ENVIRONMENT=development and DEBUG=true.
    if str(current.environment or "").strip().lower() == "development" and not current.debug:
        return ["environment_not_production"]
    if not is_production_environment(current):
        return []

    errors: list[str] = []
    if current.debug:
        errors.append("debug_enabled")
    origins = resolve_cors_origins(current.cors_allow_origins, debug=False)
    if not origins:
        errors.append("cors_not_configured")
    elif any(
        origin == "*"
        or "localhost" in origin.lower()
        or "127.0.0.1" in origin
        or "example.invalid" in origin.lower()
        or not origin.lower().startswith("https://")
        for origin in origins
    ):
        errors.append("cors_not_production_safe")
    if not current.auth_cookie_secure:
        errors.append("secure_cookie_disabled")
    hosts = [item.strip() for item in str(current.allowed_hosts or "").split(",") if item.strip()]
    if not hosts:
        errors.append("host_not_configured")
    elif any(host == "*" or contains_placeholder(host) for host in hosts):
        errors.append("host_not_production_safe")
    try:
        encryption_key = resolve_encryption_key(current.app_encryption_key, current.app_encryption_key_file)
    except ValueError:
        encryption_key = ""
    if not encryption_key:
        # Provider credentials are persisted in SQLite by the current runtime.
        # Production readiness therefore requires an out-of-band encryption key.
        # The key itself is never returned by this function.
        errors.append("encryption_key_not_configured")
    if contains_placeholder(current.invite_tokens):
        errors.append("invite_placeholder")
    if contains_placeholder(current.admin_user_ids):
        errors.append("admin_placeholder")
    if not str(current.admin_user_ids or "").strip():
        errors.append("admin_not_configured")
    if not str(current.default_llm_provider or "").strip() or not str(current.default_llm_model or "").strip():
        errors.append("llm_default_missing")
    if current.upload_max_bytes <= 0:
        errors.append("upload_limit_invalid")
    if (
        current.rate_limit <= 0
        or current.project_daily_llm_call_limit <= 0
        or current.project_daily_token_limit <= 0
        or (current.user_daily_chapter_limit_enabled and current.user_daily_chapter_limit <= 0)
    ):
        errors.append("quota_invalid")
    if str(current.log_level or "").upper() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        errors.append("log_level_invalid")
    if not str(current.data_dir or "").strip() or not str(current.upload_dir or "").strip():
        errors.append("storage_path_missing")
    return sorted(set(errors))
