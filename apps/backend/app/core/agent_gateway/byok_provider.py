"""IoC seam for per-org BYOK secrets delivery to agents.

`BytokSecretsProvider` is the async callable Protocol that `core/coding_agent`
implements and registers here at import time. `core/agent_gateway` calls the
registered provider inside `_build_config_update_dto` to populate
`AgentConfig.byok_secrets` without importing `core/byok` or `core/coding_agent`.

Canonical import direction:
  core/coding_agent → core/agent_gateway  (registers here)
  core/agent_gateway → core/agent_gateway.byok_provider  (calls it)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

# Protocol type alias: async (org_id, *, session) -> dict[str, SecretStr]
BytokSecretsProvider = Callable[
    [UUID, AsyncSession],
    Awaitable[dict[str, SecretStr]],
]

# ── Single-slot registry ───────────────────────────────────────────────

_PROVIDER: BytokSecretsProvider | None = None


def register_byok_secrets_provider(provider: BytokSecretsProvider) -> None:
    """Register the module-global byok secrets provider.

    Idempotent for the same instance; raises RuntimeError on conflicting
    re-registration so a double-wiring bug surfaces at boot.
    Tests reset via `clear_byok_secrets_provider`.
    """
    global _PROVIDER
    if _PROVIDER is not None and _PROVIDER is not provider:
        raise RuntimeError("BytokSecretsProvider already registered — clear it before re-registering")
    _PROVIDER = provider


def get_byok_secrets_provider() -> BytokSecretsProvider | None:
    """Return the registered provider, or None when not yet registered."""
    return _PROVIDER


def clear_byok_secrets_provider() -> None:
    """Reset the registry slot. Used in tests to swap stub providers."""
    global _PROVIDER
    _PROVIDER = None
