"""IoC seam for per-org API key secrets delivery to agents.

`ApiKeySecretsProvider` is the async callable Protocol that `core/coding_agent`
implements and registers here at import time. `core/agent_gateway` calls the
registered provider inside `_build_config_update_dto` to populate
`AgentConfig.api_keys` without importing `core/api_keys` or `core/coding_agent`.

Canonical import direction:
  core/coding_agent → core/agent_gateway  (registers here)
  core/agent_gateway → core/agent_gateway.api_key_provider  (calls it)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

# Protocol type alias: async (org_id, *, session) -> dict[str, SecretStr]
ApiKeySecretsProvider = Callable[
    [UUID, AsyncSession],
    Awaitable[dict[str, SecretStr]],
]

# ── Single-slot registry ───────────────────────────────────────────────

_PROVIDER: ApiKeySecretsProvider | None = None


def register_api_key_secrets_provider(provider: ApiKeySecretsProvider) -> None:
    """Register the module-global API key secrets provider.

    Idempotent for the same instance; raises RuntimeError on conflicting
    re-registration so a double-wiring bug surfaces at boot.
    Tests reset via `clear_api_key_secrets_provider`.
    """
    global _PROVIDER
    if _PROVIDER is not None and _PROVIDER is not provider:
        raise RuntimeError("ApiKeySecretsProvider already registered — clear it before re-registering")
    _PROVIDER = provider


def get_api_key_secrets_provider() -> ApiKeySecretsProvider | None:
    """Return the registered provider, or None when not yet registered."""
    return _PROVIDER


def clear_api_key_secrets_provider() -> None:
    """Reset the registry slot. Used in tests to swap stub providers."""
    global _PROVIDER
    _PROVIDER = None
