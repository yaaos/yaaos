"""Types + `IntegrationProvider` Protocol for `domain/integrations`.

Each upstream provider (Linear, Notion, future) implements `IntegrationProvider`
to surface its OAuth + hosted-MCP wiring. The Protocol stays small — provider
plugins push their config in via a registry at bootstrap so `domain/integrations`
never imports plugin code. `ProviderConfig` itself lives in `core/oauth`
because `core/oauth.exchange_code` consumes it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from app.core.oauth import ProviderConfig


@runtime_checkable
class IntegrationProvider(Protocol):
    """Provider plugin contract. `validate` runs a minimal upstream call to
    confirm the stored access token still works."""

    provider_id: str
    config: ProviderConfig

    async def validate(self, access_token: SecretStr) -> bool:
        """Minimal upstream call — returns True on 2xx, False otherwise."""
        ...


# Registry — plugins register themselves at bootstrap so domain/integrations
# stays free of plugin imports. Mirrors the core/api_keys validator-registry
# pattern.
_REGISTRY: dict[str, IntegrationProvider] = {}


def register_provider(provider: IntegrationProvider) -> None:
    _REGISTRY[provider.provider_id] = provider


def get_provider(provider_id: str) -> IntegrationProvider | None:
    return _REGISTRY.get(provider_id)


def known_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


@contextmanager
def set_providers_for_tests(
    providers: dict[str, IntegrationProvider] | None = None,
) -> Generator[dict[str, IntegrationProvider]]:
    """Test-only context manager that yields the live provider registry.

    On entry snapshots the current registry (so the test body can register stub
    providers freely); on exit restores the original entries. If `providers` is
    given, the registry is seeded with those entries before yielding.
    """
    original = dict(_REGISTRY)
    _REGISTRY.clear()
    if providers:
        _REGISTRY.update(providers)
    try:
        yield _REGISTRY
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(original)


ValidateCallable = Callable[[SecretStr], Awaitable[bool]]


class IntegrationError(Exception):
    """Base for domain/integrations errors."""


class ProviderNotRegisteredError(IntegrationError):
    pass


class IntegrationNotConnectedError(IntegrationError):
    pass


class BrokenCredentialsError(IntegrationError):
    pass


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
    "set_providers_for_tests",
]
