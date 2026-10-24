"""Single runtime provider resolution point.

The optional override exists for deterministic tests and isolated smoke runs;
normal application code resolves to the shared configured provider singleton.
"""

from __future__ import annotations

import contextvars
from typing import Any

_RUNTIME_PROVIDER: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "novelcraft_runtime_provider", default=None
)


def set_runtime_provider(provider: Any | None) -> contextvars.Token:
    """Set a provider for the current async context and return a restore token."""
    return _RUNTIME_PROVIDER.set(provider)


def reset_runtime_provider(token: contextvars.Token | None) -> None:
    if token is not None:
        try:
            _RUNTIME_PROVIDER.reset(token)
        except ValueError:
            pass


def clear_runtime_provider() -> None:
    _RUNTIME_PROVIDER.set(None)


def get_runtime_provider(legacy_provider: Any | None = None) -> Any:
    provider = _RUNTIME_PROVIDER.get()
    if provider is not None:
        return provider
    if legacy_provider is not None:
        return legacy_provider
    from app.llm.provider import llm

    return llm
