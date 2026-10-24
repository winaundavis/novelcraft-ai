"""Unified LLM provider layer for OpenAI-compatible, Anthropic and DeepSeek APIs."""

import json
import logging
import os
import time
from typing import AsyncIterator

import httpx

from app.core.config import settings
from app.db import get_active_llm_config
from app.llm.audit import get_llm_audit_context, record_llm_call, set_llm_audit_context, set_llm_call_meta
from app.llm.roles import DEFAULT_ROLE_PROFILES, ROLE_CHECK, ROLE_UNSPECIFIED

_logger = logging.getLogger(__name__)


# 单次 LLM 调用超时（秒）——按"模型 + 操作（角色）"拆分（Codex 四轮 P2-2）：
#   - 短任务（聊天 / 灵感 / 审校 / 摘要 / 核查 / 文风）保持短超时，故障请求快速失败；
#   - 长任务（正文生成 / 续写 / 扩写，max_tokens=4096）用长超时——kimi-k2.6 实测
#     生成 4096 tokens 约 8 分钟，90s 必超时 → 3 次重试 → fallback 占位 → 章节重试死循环。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
ROLE_TIMEOUT_SECONDS: dict[str, float] = {
    "writing": 600.0,   # 正文生成 / 续写 / 扩写
    "planning": 180.0,  # 大纲规划启用 thinking，90s 在真实 Arts 请求中会过早中断
    "summary": 90.0,    # 章节/卷摘要、事实抽取
    "check": 90.0,      # 事实核查 / 钩子判定 / AI 味
    "reason": 120.0,    # 冲突仲裁 / 根因分析
    "style": 90.0,      # 文风分析 / 画像抽取
    "fulltext": 120.0,  # 全文问答
}

# 熔断（Codex P2-2）：同一 provider 连续超时达到阈值后，短期内快速失败走 fallback，
# 避免"3 次长等待后才降级"拖垮生成链路。
CIRCUIT_BREAK_THRESHOLD = 2
CIRCUIT_COOLDOWN_SECONDS = 60.0

# 第 3 档长度路由：输入估算 token 超阈值且角色配置了 long_model 时切换长窗模型（默认不启用）
LONG_CONTEXT_THRESHOLD_TOKENS = 60000
DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com/v1"
ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"

# OpenAI 兼容型第三方默认 endpoint（异模型评分 / 角色级 provider 覆盖用）
KIMI_DEFAULT_BASE = "https://api.moonshot.cn/v1"
GLM_DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
QWEN_DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ARTS_DEFAULT_BASE = "https://ai.artsapi.com/v1"

# OpenAI 兼容组：统一走 OpenAI chat/completions 格式，仅 endpoint/key 不同
OPENAI_COMPAT_PROVIDERS = {"openai", "qwen", "wenxin", "kimi", "glm", "local", "arts"}


def _estimate_tokens(messages: list[dict]) -> int:
    """轻量输入 token 估算（1 汉字 ≈ 1 token 的保守近似），仅用于长度路由的粗略判断。

    中文按字符数、英文按字符数（略低估但阈值 60K 足够宽容），不追求精确计数。
    """
    total = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    total += len(str(part["text"]))
        else:
            total += len(str(content))
    return total


class LLMConfigurationError(RuntimeError):
    """Raised when no usable LLM provider has been configured."""


class LLMResponseError(RuntimeError):
    """Raised when a provider returns an invalid or unusable response."""

    error_code = "response_invalid"
    user_message = "模型返回内容无效，请稍后手动重试。"
    retryable = True


class LLMTransportError(LLMResponseError):
    """Raised when the configured provider cannot be reached."""

    error_code = "provider_unreachable"
    user_message = "无法连接模型服务，请检查网络或模型服务配置后再试。"


class LLMTimeoutError(LLMTransportError):
    """Raised when a provider request exceeds its configured timeout."""

    error_code = "provider_timeout"
    user_message = "模型服务响应超时，本次未自动重试，请稍后手动重试。"


class LLMUpstreamError(LLMResponseError):
    """Raised when a provider responds with a non-success HTTP status."""

    error_code = "provider_error"
    user_message = "模型服务拒绝了本次请求，请维护者检查模型配置或服务状态。"


class LLMBudgetExceededError(LLMResponseError):
    """Raised before provider dispatch when the project daily budget is exhausted."""

    error_code = "budget_exhausted"
    user_message = "今日项目 AI 使用额度已达到上限，模型未被调用。"
    retryable = False


class LLMReasoningBudgetExhaustedError(LLMResponseError):
    """输出预算被推理吃光：`finish_reason=length` 且正文为空。

    为什么要单独一个错误码（2026-10-19 事故）：这类失败以前被笼统归为
    `response_invalid`，前端于是显示"AI 服务暂时不可用"，把排查引向网络/密钥。
    审计 `babfeffb67b1` 的真实原因是——planning 角色开着推理，
    4096 的 max_tokens 全用在思考上，正文 0 字，大纲静默降级成本地模板。

    `reasoning_chars > 0` 表示"确实产生过推理"，这种情况重试时关掉推理是有意义的；
    纯 `finish_reason=length` 且无推理，说明预算对输出本身就不够，重试无益。
    """

    error_code = "output_budget_exhausted"
    user_message = "模型把输出预算用在了推理上，正文没有产出。请关闭该角色的推理（或提高 max_tokens）后重试。"

    def __init__(
        self,
        message: str,
        *,
        reasoning_chars: int = 0,
        finish_reason: str = "",
        completion_tokens: int = 0,
        max_tokens: int = 0,
    ) -> None:
        super().__init__(message)
        self.reasoning_chars = int(reasoning_chars or 0)
        self.finish_reason = finish_reason
        self.completion_tokens = int(completion_tokens or 0)
        self.max_tokens = int(max_tokens or 0)


def _request_failure(exc: httpx.HTTPError) -> LLMResponseError:
    # These messages can reach job state and SSE clients. httpx exception text may
    # contain private base URLs, path credentials, or query parameters.
    if isinstance(exc, httpx.TimeoutException):
        return LLMTimeoutError(LLMTimeoutError.user_message)
    if isinstance(exc, httpx.TransportError):
        return LLMTransportError(LLMTransportError.user_message)
    return LLMUpstreamError(LLMUpstreamError.user_message)


class LLMProvider:
    """Unified chat interface across supported providers."""

    def __init__(self, provider: str = "", model: str = "", api_key: str = "", api_base: str = ""):
        # 记录调用方是否显式指定 provider / key / base（用于后续不被 DB 激活配置覆盖）
        self._explicit_provider = bool(provider)
        self._explicit_api_key = bool(api_key)
        self._explicit_api_base = bool(api_base)
        self.provider = provider or settings.default_llm_provider
        self.model = model or settings.default_llm_model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = settings.default_temperature
        self.max_tokens = settings.default_max_tokens
        self.role_overrides: dict = {}
        self._config_ts = 0.0
        # 熔断状态：provider -> {consecutive: int, open_until: float}
        self._circuit: dict[str, dict] = {}
        self._refresh_runtime_config(force=True)

    def _refresh_runtime_config(self, force: bool = False) -> None:
        """Read active runtime config and cache it briefly to avoid repeated DB lookups."""
        now = time.monotonic()
        if not force and (now - self._config_ts) < 5:
            return

        self._config_ts = now
        runtime = get_active_llm_config() or {}

        # 修复：显式指定的 provider/key/base 不被 DB 激活配置覆盖。
        # 否则 LLMProvider(provider="kimi") 会被 get_active_llm_config() 的
        # deepseek 激活配置整体覆盖，导致"异模型评审"实际仍在调 DeepSeek。
        if not self._explicit_provider:
            self.provider = runtime.get("provider") or self.provider or settings.default_llm_provider
        # 读取 DB 激活配置的 apiKey：仅当 DB 激活配置的 provider 与当前 provider 一致时
        # （key 是用户在前端为该 provider 存的）；若 DB 激活的是别的 provider（如
        # deepseek）则不读，避免污染显式指定的 kimi/glm/qwen。
        if not self._explicit_api_key:
            runtime_provider = str(runtime.get("provider") or "").strip()
            if runtime_provider and runtime_provider == self.provider:
                self.api_key = runtime.get("apiKey") or self.api_key
        if not self._explicit_api_base:
            self.api_base = runtime.get("apiBase") or self.api_base
        # model 同理：DB 激活配置与当前 provider 一致时，用 DB 存的 model（如 kimi 的 kimi-k3），
        # 避免显式 provider 时误用 settings.default_llm_model 打到错误的模型名。
        runtime_provider2 = str(runtime.get("provider") or "").strip()
        if not self._explicit_provider:
            self.model = runtime.get("model") or self.model or settings.default_llm_model
        elif runtime_provider2 and runtime_provider2 == self.provider:
            self.model = runtime.get("model") or self.model or settings.default_llm_model
        self.temperature = runtime.get("temperature") or settings.default_temperature
        self.max_tokens = runtime.get("maxTokens") or settings.default_max_tokens
        raw_overrides = runtime.get("roleOverrides")
        self.role_overrides = raw_overrides if isinstance(raw_overrides, dict) else {}

        self._openai_key = ""
        self._openai_base = self.api_base or settings.openai_api_base or os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
        self._anthropic_key = ""
        self._anthropic_base = settings.anthropic_api_base or os.getenv("ANTHROPIC_API_BASE", ANTHROPIC_DEFAULT_BASE)
        self._deepseek_key = ""

        if self.provider in OPENAI_COMPAT_PROVIDERS:
            # 显式 provider 时优先用该 provider 的专用 key（kimi/glm/qwen 各自独立）；
            # 未显式指定时保留 DB 激活配置的 key 或 settings 默认。
            self._openai_key = self.api_key or self._provider_api_key(self.provider)
            # 显式 base 优先；否则用该 provider 的默认 endpoint（kimi/glm/qwen 各自不同）
            if not self._explicit_api_base:
                self._openai_base = self._provider_default_base(self.provider) or self._openai_base
        elif self.provider == "claude":
            self._anthropic_key = self.api_key or settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
        elif self.provider == "deepseek":
            self._deepseek_key = self.api_key or settings.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")

        if not any([self._openai_key, self._anthropic_key, self._deepseek_key]):
            self._openai_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY", "")
            self._anthropic_key = settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
            self._deepseek_key = settings.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
            if not self.api_base:
                self._openai_base = settings.openai_api_base or os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")

    def is_configured(self) -> bool:
        return bool(self._openai_key or self._anthropic_key or self._deepseek_key)

    def _circuit_open(self, provider: str) -> bool:
        """该 provider 是否处于熔断打开状态（连续超时后冷却期内快速失败）。"""
        state = self._circuit.get(provider)
        if not state:
            return False
        if state.get("open_until") and time.monotonic() < state["open_until"]:
            return True
        return False

    def _record_circuit_success(self, provider: str) -> None:
        """请求成功 → 清零熔断计数。"""
        self._circuit.pop(provider, None)

    def _record_circuit_timeout(self, provider: str) -> None:
        """请求超时 → 计数 +1，达到阈值后打开熔断（冷却期内快速失败）。"""
        state = self._circuit.setdefault(provider, {"consecutive": 0, "open_until": 0.0})
        state["consecutive"] = state.get("consecutive", 0) + 1
        if state["consecutive"] >= CIRCUIT_BREAK_THRESHOLD:
            state["open_until"] = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS

    def assert_configured(self) -> None:
        self._refresh_runtime_config(force=True)
        if not self.is_configured():
            raise LLMConfigurationError(
                "No LLM provider is configured. Please set an API key for OpenAI, Anthropic, or DeepSeek."
            )

    async def chat(self, messages: list[dict], role: str | None = None, **kwargs) -> str:
        """统一聊天入口。`role` 为任务角色（见 app.llm.roles），未提供时回退激活配置。

        向后兼容：不传 role 时行为与旧版完全一致（走激活配置）。
        """
        self.assert_configured()
        return await self._real_chat(messages, role=role, **kwargs)

    async def chat_for(self, role: str, messages: list[dict], **kwargs) -> str:
        """按任务角色路由的便捷入口：`llm.chat_for(ROLE_CHECK, messages, ...)`。"""
        return await self.chat(messages, role=role, **kwargs)

    async def chat_stream(self, messages: list[dict], role: str | None = None, **kwargs) -> AsyncIterator[str]:
        """流式聊天入口，`role` 语义与 `chat` 一致。"""
        self.assert_configured()
        async for chunk in self._real_chat_stream(messages, role=role, **kwargs):
            yield chunk

    def _resolve_role_profile(self, role: str | None) -> dict:
        """合并 DB 角色覆盖与代码默认档位。

        返回 {provider, model, temperature, api_key, api_base, long_model}：
        - api_key / api_base：第 2 档角色独立 key/base（role_overrides 配置，空则 None）
        - long_model：第 3 档长度路由的长窗模型（空串表示不启用）
        未配置/未知角色返回空 dict（完全回退激活配置）。
        """
        if not role:
            return {}
        role_cfg = {}
        if isinstance(self.role_overrides, dict):
            candidate = self.role_overrides.get(role)
            if isinstance(candidate, dict):
                role_cfg = candidate
        defaults = DEFAULT_ROLE_PROFILES.get(role, {})
        provider = str(role_cfg.get("provider") or "").strip()
        model = str(role_cfg.get("model") or "").strip()
        if not model:
            model = str(defaults.get("model") or "").strip()
        temperature = role_cfg.get("temperature")
        if temperature is None:
            temperature = defaults.get("temperature")
        api_key = str(role_cfg.get("api_key") or role_cfg.get("apiKey") or "").strip() or None
        api_base = str(role_cfg.get("api_base") or role_cfg.get("apiBase") or "").strip() or None
        long_model = str(role_cfg.get("long_model") or role_cfg.get("longModel") or "").strip()
        thinking = str(role_cfg.get("thinking") or defaults.get("thinking") or "").strip().lower()
        # 角色的输出上限**下限**（0 表示不设下限）
        raw_max_tokens = role_cfg.get("max_tokens", defaults.get("max_tokens"))
        try:
            role_max_tokens = int(raw_max_tokens) if raw_max_tokens else 0
        except (TypeError, ValueError):
            role_max_tokens = 0
        return {
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "api_key": api_key,
            "api_base": api_base,
            "long_model": long_model,
            "thinking": thinking if thinking in {"enabled", "disabled"} else "",
            "max_tokens": max(role_max_tokens, 0),
        }

    def _resolve_max_tokens(self, profile: dict, kwargs: dict) -> int:
        """解析本次调用的输出上限。

        优先级：显式 `kwargs["max_tokens"]` > `max(角色下限, 全局配置)`。

        **角色值只抬下限、绝不压低**全局配置：否则改默认值会把作者已经调高的预算砍掉。

        为什么正文角色要下限（2026-10-20）：全局 4096 对 4000+ 字的长章节不够，
        `finish_reason=length` 的正文此前被当成成功结果静默落库 ——
        作者拿到半句话却以为写完了。实测 provider 接受 8192/16384，故设 8192 下限。
        """
        explicit = kwargs.get("max_tokens")
        if explicit:
            try:
                return int(explicit)
            except (TypeError, ValueError):
                pass
        role_floor = 0
        if isinstance(profile, dict):
            try:
                role_floor = int(profile.get("max_tokens") or 0)
            except (TypeError, ValueError):
                role_floor = 0
        return max(role_floor, int(self.max_tokens))

    @staticmethod
    def _thinking_request_policy(profile: dict, kwargs: dict) -> dict:
        """Translate a role-level policy into an explicit provider request policy."""
        thinking = str(kwargs.get("thinking") or profile.get("thinking") or "").strip().lower()
        if thinking == "disabled":
            return {"thinking": {"type": "disabled"}, "reasoning_effort": "none"}
        if thinking == "enabled":
            return {"thinking": {"type": "enabled"}}
        return {}

    def request_manifest(self, role: str | None, **kwargs) -> dict:
        """Return the resolved, secret-free request parameters without calling a provider."""
        self._refresh_runtime_config(force=True)
        profile = self._resolve_role_profile(role)
        policy = self._thinking_request_policy(profile, kwargs)
        model = kwargs.get("model") or profile.get("model") or self.model
        temperature = kwargs.get("temperature", profile.get("temperature"))
        if temperature is None:
            temperature = self.temperature
        max_tokens = self._resolve_max_tokens(profile, kwargs)

        if self._explicit_provider:
            source = "explicit provider configuration"
        else:
            runtime = get_active_llm_config() or {}
            if runtime:
                source = "active database LLM configuration"
            elif os.getenv("DEFAULT_LLM_MODEL"):
                source = "environment DEFAULT_LLM_MODEL"
            else:
                source = "settings default"
        if isinstance(self.role_overrides.get(role or ""), dict) and self.role_overrides[role or ""].get("model"):
            source = f"active database role override ({role})"

        return {
            "provider": self.provider,
            "model": model,
            "temperature": float(temperature),
            "max_tokens": max_tokens,
            "thinking": policy.get("thinking"),
            "reasoning_effort": policy.get("reasoning_effort"),
            "config_source": source,
        }

    def _provider_api_key(self, provider: str) -> str:
        """解析指定 provider 的可用 API key（角色 provider 覆盖用）。

        修复：kimi/glm/qwen 使用各自专用 key；未配置时返回空串（明确"未配置"），
        绝不 fallback 到 openai/deepseek key —— 否则会用错 key 打错 endpoint（401）。
        """
        if provider == "kimi":
            return self.api_key or (settings.kimi_api_key or "").strip() or os.getenv("KIMI_API_KEY", "").strip()
        if provider == "glm":
            return self.api_key or (settings.glm_api_key or "").strip() or os.getenv("GLM_API_KEY", "").strip()
        if provider == "qwen":
            return self.api_key or (settings.qwen_api_key or "").strip() or os.getenv("QWEN_API_KEY", "").strip()
        if provider == "arts":
            return self.api_key or (settings.arts_api_key or "").strip() or os.getenv("ARTS_API_KEY", "").strip()
        if provider == "wenxin":
            return self.api_key or os.getenv("WENXIN_API_KEY", "").strip()
        if provider in OPENAI_COMPAT_PROVIDERS:
            return self.api_key or (settings.openai_api_key or "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
        if provider == "claude":
            return self.api_key or settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if provider == "deepseek":
            return self.api_key or settings.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
        return ""

    def _provider_default_base(self, provider: str) -> str:
        """返回指定 OpenAI 兼容 provider 的默认 endpoint（无则空串走 openai 默认）。"""
        if provider == "kimi":
            return settings.kimi_api_base or os.getenv("KIMI_API_BASE", KIMI_DEFAULT_BASE)
        if provider == "glm":
            return settings.glm_api_base or os.getenv("GLM_API_BASE", GLM_DEFAULT_BASE)
        if provider == "qwen":
            return settings.qwen_api_base or os.getenv("QWEN_API_BASE", QWEN_DEFAULT_BASE)
        if provider == "arts":
            return settings.arts_api_base or os.getenv("ARTS_API_BASE", ARTS_DEFAULT_BASE)
        if provider == "deepseek":
            return DEEPSEEK_DEFAULT_BASE
        return ""

    def _provider_api_base(self, provider: str) -> str:
        if provider in OPENAI_COMPAT_PROVIDERS:
            return self.api_base or self._provider_default_base(provider) or settings.openai_api_base or os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
        if provider == "deepseek":
            return DEEPSEEK_DEFAULT_BASE
        if provider == "claude":
            return self._anthropic_base
        return ""

    async def _call_with_provider(
        self, provider: str, api_key: str, messages: list[dict], model: str, temperature: float, max_tokens: int,
        api_base: str = "", timeout: float | None = None, circuit_provider: str | None = None,
        request_policy: dict | None = None,
    ) -> str:
        """按角色指定的 provider 调用（临时切换通道，用完恢复，不污染全局状态）。

        api_base：第 2 档角色独立 base；为空时用该 provider 的默认 base。
        timeout：单次调用超时（秒），None 时用角色/默认超时。
        circuit_provider：熔断记录用的**实际 provider**（Codex P1-1：角色路由后与激活可能不同）。
        """
        if provider in OPENAI_COMPAT_PROVIDERS:
            saved_base, saved_key = self._openai_base, self._openai_key
            try:
                self._openai_base = api_base or self._provider_api_base(provider)
                self._openai_key = api_key
                # 模型兼容性：Moonshot kimi-k3 只允许 temperature=1（其他值返回 400）
                if provider == "kimi":
                    temperature = 1.0
                return await self._call_openai(messages, model, temperature, max_tokens, timeout=timeout, provider=circuit_provider or provider, request_policy=request_policy)
            finally:
                self._openai_base, self._openai_key = saved_base, saved_key
        if provider == "claude":
            saved_base, saved_key = self._anthropic_base, self._anthropic_key
            try:
                self._anthropic_base = api_base or ANTHROPIC_DEFAULT_BASE
                self._anthropic_key = api_key
                return await self._call_anthropic(messages, model, temperature, max_tokens, timeout=timeout, provider=circuit_provider or provider)
            finally:
                self._anthropic_base, self._anthropic_key = saved_base, saved_key
        if provider == "deepseek":
            saved_base, saved_key = self._openai_base, self._openai_key
            try:
                self._openai_base = api_base or DEEPSEEK_DEFAULT_BASE
                self._openai_key = api_key
                return await self._call_openai(messages, model or "deepseek-v4-flash", temperature, max_tokens, timeout=timeout, provider=circuit_provider or provider, request_policy=request_policy)
            finally:
                self._openai_base, self._openai_key = saved_base, saved_key
        raise LLMConfigurationError(f"不支持的模型提供方：{provider}")

    async def _stream_with_provider(
        self, provider: str, api_key: str, messages: list[dict], model: str, temperature: float, max_tokens: int,
        api_base: str = "", timeout: float | None = None, circuit_provider: str | None = None,
        request_policy: dict | None = None,
    ) -> AsyncIterator[str]:
        """按角色指定的 provider 流式调用；非 OpenAI 兼容通道退化为非流式分片。"""
        if provider in OPENAI_COMPAT_PROVIDERS:
            saved_base, saved_key = self._openai_base, self._openai_key
            try:
                self._openai_base = api_base or self._provider_api_base(provider)
                self._openai_key = api_key
                async for chunk in self._stream_openai(messages, model, temperature, max_tokens, timeout=timeout, provider=circuit_provider or provider, request_policy=request_policy):
                    yield chunk
            finally:
                self._openai_base, self._openai_key = saved_base, saved_key
            return
        full_text = await self._call_with_provider(provider, api_key, messages, model, temperature, max_tokens, api_base=api_base, timeout=timeout, circuit_provider=circuit_provider, request_policy=request_policy)
        for index in range(0, len(full_text), 12):
            yield full_text[index:index + 12]

    async def _real_chat(self, messages: list[dict], role: str | None = None, **kwargs) -> str:
        """统一聊天入口，并在"推理吃光输出预算"时**自动重试一次**。

        2026-10-19 事故（审计 `babfeffb67b1`）：planning 开着 reasoning，4096 的
        max_tokens 全用在思考上，正文 0 字 → 大纲静默降级成本地模板，
        前端却显示"AI 服务暂时不可用"。

        重试策略刻意收窄：只有**确实产生过推理**（`reasoning_chars > 0`）才重试，
        且重试必须真的关掉推理。纯 `finish_reason=length`（预算对输出本身就不够）
        重试无益，直接抛可诊断的错误给调用方。
        """
        try:
            return await self._real_chat_once(messages, role=role, **kwargs)
        except LLMReasoningBudgetExhaustedError as exc:
            already_disabled = str(kwargs.get("thinking") or "").strip().lower() == "disabled"
            if exc.reasoning_chars <= 0 or already_disabled:
                raise
            _logger.warning(
                "模型推理耗尽输出预算（reasoning_chars=%s, finish_reason=%s, role=%s），"
                "自动以 thinking=disabled 重试一次。",
                exc.reasoning_chars, exc.finish_reason, role or "",
            )
            return await self._real_chat_once(messages, role=role, **{**kwargs, "thinking": "disabled"})

    async def _real_chat_once(self, messages: list[dict], role: str | None = None, **kwargs) -> str:
        from app.services.project_budget_service import consume_project_budget
        consume_project_budget(_estimate_tokens(messages) + int(kwargs.get("max_tokens", self.max_tokens)))
        self._refresh_runtime_config()
        # 未显式传 role 时，回退用审计上下文 role 路由（Codex 八轮）：保证角色路由与审计一致——
        # 例如 import_service/review_service 只设置审计 role（word/check/style）而未传 llm.chat(role=...)，
        # 若不回退则永远走激活配置，role_overrides 的角色路由形同虚设。
        # P2（Codex 复测）：仍无 role 时记录为 unspecified（不静默归为 check），
        # 避免遗漏 role 的写作请求被错误路由到审校模型；并产生结构化 warning。
        if not role:
            role = get_llm_audit_context().get("role") or ROLE_UNSPECIFIED
            if role == ROLE_UNSPECIFIED:
                _logger.warning(
                    "LLM 调用未指定 role，已记录为 unspecified（不静默归为 check）——请检查调用点是否遗漏 role。"
                )
        # P1（Codex 复测）：把解析后的 role 写回审计上下文，确保 _call_openai/_call_with_provider
        # 内 record_llm_call 记录到正确 role（此前 ctx.role 为空导致 682 次空 role 审计）。
        if role:
            set_llm_audit_context(role=role)
        profile = self._resolve_role_profile(role)
        request_policy = self._thinking_request_policy(profile, kwargs)
        model = kwargs.get("model") or profile.get("model") or self.model
        if "temperature" in kwargs:
            temperature = kwargs["temperature"]
        elif profile.get("temperature") is not None:
            temperature = profile["temperature"]
        else:
            temperature = self.temperature
        max_tokens = self._resolve_max_tokens(profile, kwargs)
        # 按角色拆分超时（Codex P2-2）：短任务短超时、长任务长超时；显式 kwargs 优先
        timeout = kwargs.get("timeout")
        if timeout is None:
            timeout = ROLE_TIMEOUT_SECONDS.get(role or "", DEFAULT_REQUEST_TIMEOUT_SECONDS)

        role_provider = profile.get("provider") or ""
        role_api_key = profile.get("api_key") or ""
        role_api_base = profile.get("api_base") or ""
        # 先解析**实际生效的 provider**（角色路由后），熔断按它检查/记录（Codex P1-1）
        effective_provider = self.provider
        if role_api_key:
            effective_provider = role_provider or self.provider
        elif role_provider and role_provider != self.provider and self._provider_api_key(role_provider):
            effective_provider = role_provider

        # 熔断：按实际 provider 检查（多模型路由下语义正确）
        if self._circuit_open(effective_provider):
            raise LLMTransportError(f"LLM 提供方 {effective_provider} 连续超时熔断中")

        # 第 3 档长度路由（轻量）：角色配置了 long_model 且输入超阈值 → 切换长窗模型
        long_model = profile.get("long_model") or ""
        if long_model and _estimate_tokens(messages) > LONG_CONTEXT_THRESHOLD_TOKENS:
            model = long_model

        # ① 第 2 档：角色配置了独立 API key → 用该角色的独立 key/base 走对应通道
        #    （即使 provider 与激活相同也走独立 key；provider 未配时沿用激活 provider）
        if role_api_key:
            return await self._call_with_provider(
                role_provider or self.provider,
                role_api_key,
                messages,
                model,
                temperature,
                max_tokens,
                api_base=role_api_base,
                timeout=timeout,
                circuit_provider=effective_provider,
                request_policy=request_policy,
            )

        # ② 角色指定了不同 provider 且全局 key 可用 → 走角色通道（临时切换，向后兼容）
        if role_provider and role_provider != self.provider:
            role_key = self._provider_api_key(role_provider)
            if role_key:
                return await self._call_with_provider(
                    role_provider, role_key, messages, model, temperature, max_tokens,
                    timeout=timeout, circuit_provider=effective_provider,
                    request_policy=request_policy,
                )

        if self._openai_key:
            # 模型兼容性：kimi-k3 只允许 temperature=1
            if self.provider == "kimi":
                temperature = 1.0
            return await self._call_openai(messages, model, temperature, max_tokens, timeout=timeout, provider=effective_provider, request_policy=request_policy)
        if self._anthropic_key:
            return await self._call_anthropic(messages, model, temperature, max_tokens, timeout=timeout, provider=effective_provider)
        if self._deepseek_key:
            return await self._call_deepseek(messages, model, temperature, max_tokens, timeout=timeout, provider=effective_provider, request_policy=request_policy)
        raise LLMConfigurationError("No supported LLM provider is available.")

    async def _real_chat_stream(self, messages: list[dict], role: str | None = None, **kwargs) -> AsyncIterator[str]:
        from app.services.project_budget_service import consume_project_budget
        self._refresh_runtime_config()
        # 同 _real_chat：未显式传 role 时回退用审计上下文 role 路由（Codex 八轮）
        # P2（Codex 复测）：仍无 role 时记录为 unspecified（不静默归为 check）+ 结构化 warning。
        if not role:
            role = get_llm_audit_context().get("role") or ROLE_UNSPECIFIED
            if role == ROLE_UNSPECIFIED:
                _logger.warning(
                    "LLM 流式调用未指定 role，已记录为 unspecified（不静默归为 check）。"
                )
        # P1（Codex 复测）：写回审计上下文，流式 record_llm_call 记录正确 role。
        if role:
            set_llm_audit_context(role=role)
        profile = self._resolve_role_profile(role)
        request_policy = self._thinking_request_policy(profile, kwargs)
        model = kwargs.get("model") or profile.get("model") or self.model
        if "temperature" in kwargs:
            temperature = kwargs["temperature"]
        elif profile.get("temperature") is not None:
            temperature = profile["temperature"]
        else:
            temperature = self.temperature
        max_tokens = self._resolve_max_tokens(profile, kwargs)
        # 按角色拆分超时（Codex P2-2）
        timeout = kwargs.get("timeout")
        if timeout is None:
            timeout = ROLE_TIMEOUT_SECONDS.get(role or "", DEFAULT_REQUEST_TIMEOUT_SECONDS)

        role_provider = profile.get("provider") or ""
        role_api_key = profile.get("api_key") or ""
        role_api_base = profile.get("api_base") or ""
        # 先解析**实际生效的 provider**（角色路由后），熔断按它检查（Codex P1-1）
        effective_provider = self.provider
        if role_api_key:
            effective_provider = role_provider or self.provider
        elif role_provider and role_provider != self.provider and self._provider_api_key(role_provider):
            effective_provider = role_provider

        # 熔断：按实际 provider 检查
        if self._circuit_open(effective_provider):
            raise LLMTransportError(f"LLM 提供方 {effective_provider} 连续超时熔断中")

        # 第 3 档长度路由（轻量）：角色配置了 long_model 且输入超阈值 → 切换长窗模型
        long_model = profile.get("long_model") or ""
        if long_model and _estimate_tokens(messages) > LONG_CONTEXT_THRESHOLD_TOKENS:
            model = long_model

        # ① 第 2 档：角色配置了独立 API key → 用该角色的独立 key/base 走对应通道
        if role_api_key:
            consume_project_budget(_estimate_tokens(messages) + int(max_tokens))
            async for chunk in self._stream_with_provider(
                role_provider or self.provider,
                role_api_key,
                messages,
                model,
                temperature,
                max_tokens,
                api_base=role_api_base,
                timeout=timeout,
                circuit_provider=effective_provider,
                request_policy=request_policy,
            ):
                yield chunk
            return

        # ② 角色指定了不同 provider 且全局 key 可用 → 走角色通道（临时切换，向后兼容）
        if role_provider and role_provider != self.provider:
            role_key = self._provider_api_key(role_provider)
            if role_key:
                consume_project_budget(_estimate_tokens(messages) + int(max_tokens))
                async for chunk in self._stream_with_provider(
                    role_provider, role_key, messages, model, temperature, max_tokens,
                    timeout=timeout, circuit_provider=effective_provider,
                    request_policy=request_policy,
                ):
                    yield chunk
                return

        if self._openai_key:
            consume_project_budget(_estimate_tokens(messages) + int(max_tokens))
            # 模型兼容性：kimi-k3 只允许 temperature=1
            if self.provider == "kimi":
                temperature = 1.0
            async for chunk in self._stream_openai(messages, model, temperature, max_tokens, timeout=timeout, provider=effective_provider, request_policy=request_policy):
                yield chunk
            return

        # The non-native fallback reserves its own budget in _real_chat.
        full_text = await self._real_chat(messages, role=role, **kwargs)
        for index in range(0, len(full_text), 12):
            yield full_text[index:index + 12]

    def _request_metrics(self, messages: list[dict], temperature: float, max_tokens: int) -> dict:
        """请求侧脱敏指标（Codex 八轮）：消息数 / 总字符 / 估算 token / max_tokens / 温度。

        只汇总长度类指标，不包含消息正文。
        """
        return {
            "message_count": len(messages),
            "input_chars": _estimate_tokens(messages),
            "est_tokens": _estimate_tokens(messages),
            "max_tokens": int(max_tokens or 0),
            "temperature": float(temperature or 0.0),
        }

    def _maybe_export_diag_body(self, messages: list[dict], model: str, temperature: float, max_tokens: int, provider: str) -> None:
        """诊断模式：把完整请求体落盘，供"完全相同的 app 请求体"等价对照脚本使用。

        仅当环境变量 `LLM_DIAG_BODY_DIR` 指向的目录存在时开启；body 含正文，只用于
        本地诊断（`_diag_kimi.py` 等），不进审计表、不进 git；默认关闭。
        """
        diag_dir = os.environ.get("LLM_DIAG_BODY_DIR", "")
        if not diag_dir:
            return
        try:
            os.makedirs(diag_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            payload = {
                "provider": provider,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "message_count": len(messages),
                "messages": messages,
            }
            path = os.path.join(diag_dir, f"_diag_body_{provider}_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def _call_openai(self, messages: list[dict], model: str, temperature: float, max_tokens: int, timeout: float | None = None, provider: str | None = None, request_policy: dict | None = None) -> str:
        # 熔断记录用**实际 provider**（Codex P1-1：角色路由后与 self.provider 可能不同）
        circuit_provider = provider or self.provider
        effective_timeout = timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT_SECONDS
        start = time.monotonic()
        # 请求侧脱敏指标 + 诊断 body 导出（Codex 八轮）
        req_metrics = self._request_metrics(messages, temperature, max_tokens)
        self._maybe_export_diag_body(messages, model, temperature, max_tokens, circuit_provider)
        http_status = 0
        request_id = ""
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        effective_policy = dict(request_policy or {})
        if (
            circuit_provider == "arts"
            and "thinking" not in effective_policy
            and "reasoning_effort" not in effective_policy
        ):
            effective_policy.update({
                "thinking": {"type": "disabled"},
                "reasoning_effort": "none",
            })
        body.update(effective_policy)
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await client.post(
                    f"{self._openai_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._openai_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                http_status = response.status_code
                # Moonshot 等返回 request id（脱敏：只存 id，不存正文/密钥）
                request_id = (
                    response.headers.get("x-moonshot-request-id")
                    or response.headers.get("x-request-id")
                    or ""
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            # 熔断：连续超时后冷却期内快速失败（Codex P2-2），按实际 provider 记录
            if isinstance(exc, httpx.TimeoutException):
                self._record_circuit_timeout(circuit_provider)
                audit_id = record_llm_call(
                    provider=circuit_provider, model=model,
                    source="llm", status="timeout", duration_ms=duration_ms,
                    http_status=http_status, request_id=request_id, **req_metrics,
                )
            else:
                audit_id = record_llm_call(
                    provider=circuit_provider, model=model,
                    source="llm", status="error", duration_ms=duration_ms,
                    http_status=http_status, request_id=request_id, **req_metrics,
                )
            set_llm_call_meta(circuit_provider, model, audit_id or "")
            raise _request_failure(exc) from exc

        # 响应侧脱敏元数据（Codex 八轮）：finish_reason / usage / reasoning_chars
        finish_reason = ""
        reasoning_chars = 0
        usage: dict = {}
        try:
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content")
            finish_reason = str(choice.get("finish_reason") or "")
            reasoning = message.get("reasoning_content") or ""
            reasoning_chars = len(reasoning) if reasoning else 0
        except (KeyError, IndexError, TypeError) as exc:
            audit_id = record_llm_call(
                provider=circuit_provider, model=model,
                source="llm", status="error", duration_ms=int((time.monotonic() - start) * 1000),
                http_status=http_status, finish_reason=finish_reason,
                reasoning_chars=reasoning_chars, request_id=request_id, **req_metrics,
            )
            set_llm_call_meta(circuit_provider, model, audit_id or "")
            raise LLMResponseError("OpenAI 兼容响应缺少助手内容。") from exc
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        # 缓存命中量只取 provider 真实回传值；未回传保持 None（= 未知，不当成 0）。
        raw_cache_hit = usage.get("prompt_cache_hit_tokens")
        cache_hit_tokens = int(raw_cache_hit) if raw_cache_hit is not None else None
        if total_tokens > 0:
            from app.services.project_budget_service import settle_project_budget

            settle_project_budget(req_metrics["est_tokens"] + int(max_tokens), total_tokens)

        if not content:
            audit_id = record_llm_call(
                provider=circuit_provider, model=model,
                source="llm", status="error", duration_ms=int((time.monotonic() - start) * 1000),
                http_status=http_status, finish_reason=finish_reason,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, cache_hit_tokens=cache_hit_tokens,
                reasoning_chars=reasoning_chars,
                request_id=request_id, **req_metrics,
            )
            set_llm_call_meta(circuit_provider, model, audit_id or "")
            # 区分"真空响应"（协议问题）与"推理/预算耗尽"（可诊断的预算问题）
            if finish_reason == "length" or (reasoning_chars and not content):
                raise LLMReasoningBudgetExhaustedError(
                    f"OpenAI 兼容响应返回了空内容：finish_reason={finish_reason}, "
                    f"reasoning_chars={reasoning_chars}, completion_tokens={completion_tokens}, "
                    f"max_tokens={max_tokens}——推理内容可能耗尽 token 预算。",
                    reasoning_chars=reasoning_chars,
                    finish_reason=finish_reason,
                    completion_tokens=completion_tokens,
                    max_tokens=max_tokens,
                )
            raise LLMResponseError("OpenAI 兼容响应返回了空内容。")
        self._record_circuit_success(circuit_provider)
        audit_id = record_llm_call(
            provider=circuit_provider, model=model,
            source="llm", status="success", duration_ms=int((time.monotonic() - start) * 1000),
            output_chars=len(content),
            http_status=http_status, finish_reason=finish_reason,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, cache_hit_tokens=cache_hit_tokens,
            reasoning_chars=reasoning_chars,
            request_id=request_id, **req_metrics,
        )
        set_llm_call_meta(circuit_provider, model, audit_id or "", finish_reason=finish_reason)
        return content

    async def _stream_openai(self, messages: list[dict], model: str, temperature: float, max_tokens: int, timeout: float | None = None, provider: str | None = None, request_policy: dict | None = None) -> AsyncIterator[str]:
        # 熔断记录用**实际 provider**（Codex P1-1）
        circuit_provider = provider or self.provider
        effective_timeout = timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT_SECONDS
        start = time.monotonic()
        output_chars = 0
        finish_reason = ""
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        effective_policy = dict(request_policy or {})
        if (
            circuit_provider == "arts"
            and "thinking" not in effective_policy
            and "reasoning_effort" not in effective_policy
        ):
            effective_policy.update({
                "thinking": {"type": "disabled"},
                "reasoning_effort": "none",
            })
        body.update(effective_policy)
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self._openai_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._openai_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload == "[DONE]":
                            # 流式正常结束 → 熔断成功复位（Codex P1-1）+ 审计成功
                            self._record_circuit_success(circuit_provider)
                            audit_id = record_llm_call(
                                provider=circuit_provider, model=model,
                                source="llm", status="success",
                                duration_ms=int((time.monotonic() - start) * 1000),
                                output_chars=output_chars,
                                finish_reason=finish_reason,
                            )
                            set_llm_call_meta(circuit_provider, model, audit_id or "", finish_reason=finish_reason)
                            return
                        try:
                            choice = json.loads(payload)["choices"][0]
                            delta = choice.get("delta", {})
                            content = delta.get("content", "")
                            finish_reason = str(choice.get("finish_reason") or finish_reason)
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
                        if content:
                            output_chars += len(content)
                            yield content
        except httpx.HTTPError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            if isinstance(exc, httpx.TimeoutException):
                self._record_circuit_timeout(circuit_provider)
                record_llm_call(
                    provider=circuit_provider, model=model,
                    source="llm", status="timeout", duration_ms=duration_ms,
                )
            else:
                record_llm_call(
                    provider=circuit_provider, model=model,
                    source="llm", status="error", duration_ms=duration_ms,
                )
            raise _request_failure(exc) from exc

    async def _call_anthropic(self, messages: list[dict], model: str, temperature: float, max_tokens: int, timeout: float | None = None, provider: str | None = None) -> str:
        circuit_provider = provider or self.provider
        system_message = ""
        chat_messages = []
        for message in messages:
            if message["role"] == "system":
                system_message = message["content"]
            else:
                chat_messages.append(message)

        effective_timeout = timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT_SECONDS
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                base = self._anthropic_base.rstrip("/")
                url = f"{base}/v1/messages" if not base.endswith("/v1") else f"{base}/messages"
                response = await client.post(
                    url,
                    headers={
                        "x-api-key": self._anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model or "claude-3-5-sonnet-20241022",
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "system": system_message,
                        "messages": chat_messages,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            if isinstance(exc, httpx.TimeoutException):
                self._record_circuit_timeout(circuit_provider)
                record_llm_call(
                    provider=circuit_provider, model=model,
                    source="llm", status="timeout", duration_ms=duration_ms,
                )
            else:
                record_llm_call(
                    provider=circuit_provider, model=model,
                    source="llm", status="error", duration_ms=duration_ms,
                )
            raise _request_failure(exc) from exc

        try:
            content = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            record_llm_call(
                provider=circuit_provider, model=model,
                source="llm", status="error", duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise LLMResponseError("Anthropic 响应缺少助手内容。") from exc

        if not content:
            record_llm_call(
                provider=circuit_provider, model=model,
                source="llm", status="error", duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise LLMResponseError("Anthropic 响应返回了空内容。")
        self._record_circuit_success(circuit_provider)
        record_llm_call(
            provider=circuit_provider, model=model,
            source="llm", status="success", duration_ms=int((time.monotonic() - start) * 1000),
            output_chars=len(content),
        )
        return content

    async def _call_deepseek(self, messages: list[dict], model: str, temperature: float, max_tokens: int, timeout: float | None = None, provider: str | None = None, request_policy: dict | None = None) -> str:
        circuit_provider = provider or self.provider
        original_base = self._openai_base
        original_key = self._openai_key
        self._openai_base = "https://api.deepseek.com/v1"
        self._openai_key = self._deepseek_key
        try:
            return await self._call_openai(messages, model or "deepseek-v4-flash", temperature, max_tokens, timeout=timeout, provider=circuit_provider, request_policy=request_policy)
        finally:
            self._openai_base = original_base
            self._openai_key = original_key


llm = LLMProvider()
