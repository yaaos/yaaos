"""Types + `IntegrationProvider` Protocol for `domain/integrations`.

Each upstream provider (Linear, Notion, future) implements `IntegrationProvider`
to surface its OAuth + hosted-MCP wiring. The Protocol stays small — provider
plugins push their config in via a registry at bootstrap so `domain/integrations`
never imports plugin code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """OAuth + MCP wiring for one upstream. Filled in by the provider plugin
    at bootstrap. Env-var overrides (LINEAR_OAUTH_AUTHORIZE_URL etc.) let the
    test compose swap in the local fakes; production defaults are the real
    upstream URLs."""

    authorize_url: str
    token_url: str
    refresh_url: str
    mcp_url: str
    client_id: str
    client_secret: str
    scope_separator: str  # " " for Linear, " " for Notion (changes per provider)
    default_scopes: tuple[str, ...]
    known_read_tools: tuple[str, ...]
    known_write_tools: tuple[str, ...]


@runtime_checkable
class IntegrationProvider(Protocol):
    """Provider plugin contract. `validate` runs a minimal upstream call to
    confirm the stored access token still works (the hourly health-check
    job calls it; the BYOK-style 'Test' button calls it)."""

    provider_id: str
    config: ProviderConfig

    async def validate(self, access_token: str) -> bool:
        """Minimal upstream call — returns True on 2xx, False otherwise."""
        ...


# Registry — plugins register themselves at bootstrap so domain/integrations
# stays free of plugin imports. Mirrors the M03 core/byok validator-registry
# pattern.
_REGISTRY: dict[str, IntegrationProvider] = {}


def register_provider(provider: IntegrationProvider) -> None:
    _REGISTRY[provider.provider_id] = provider


def get_provider(provider_id: str) -> IntegrationProvider | None:
    return _REGISTRY.get(provider_id)


def known_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


# Type alias for the validate callable so call sites can refer to it without
# importing the full Protocol.
ValidateCallable = Callable[[str], Awaitable[bool]]


# Errors surfaced by domain/integrations service + the MCP proxy. Stay close
# to the Protocol so consumers can `from app.domain.integrations import ...`.


class IntegrationError(Exception):
    """Base for domain/integrations errors."""


class ProviderNotRegisteredError(IntegrationError):
    pass


class IntegrationNotConnectedError(IntegrationError):
    pass


class BrokenCredentialsError(IntegrationError):
    pass


# Convenience: keep field default factories grouped — pydantic doesn't
# play nicely with frozen dataclasses' default_factory, hence the manual list.
__all__ = [
    "BrokenCredentialsError",
    "IntegrationError",
    "IntegrationNotConnectedError",
    "IntegrationProvider",
    "ProviderConfig",
    "ProviderNotRegisteredError",
    "ValidateCallable",
    "get_provider",
    "known_providers",
    "register_provider",
]

# Silence unused-import linting for the dataclass-field re-export hook.
_ = field
