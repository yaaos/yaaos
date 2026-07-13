"""API key secrets provider, ConfigUpdate hydrator, and on-change wiring.

`build_api_key_secrets_for_org` is the api-key-secrets-provider callable registered
with `core/agent_gateway` via the IoC seam.  Called at claim time by
`_config_update_hydrator` to inject the current per-org API key secrets into the
outbound ConfigUpdate DTO — credentials are never stored in `agent_commands` at rest.

`_config_update_hydrator` is the `CommandHydrator` registered for the
`ConfigUpdate` kind.  It runs inside `claim_next` and returns the COMPLETE
outbound payload dict with `config.api_keys` populated from the live secrets.

`_register_api_key_on_change` registers the ConfigUpdate fan-out callback with
`core/api_keys` at module load time (called from `core/coding_agent/__init__.py`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    HydrationContext,
)
from app.core.agent_gateway import (
    enqueue_config_update_for_all_org_agents as _enqueue_config_update_for_all_org_agents,
)


async def build_api_key_secrets_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> dict[str, SecretStr]:
    """Return a dict of provider_id → SecretStr for every key stored for the org.

    Forwards all stored org keys — the agent-side env maps act as the allowlist;
    unknown providers are ignored there by design. `get_all_for_org` fetches and
    decrypts every row in one query, wrapping each plaintext in SecretStr on
    emergence so it never crosses module boundaries as a bare string.

    Called at claim time by `_config_update_hydrator` via the registered
    api-key-secrets-provider IoC seam.
    """
    import app.core.api_keys as _api_keys  # noqa: PLC0415

    return await _api_keys.get_all_for_org(org_id, session=session)


async def _config_update_hydrator(
    payload: dict[str, Any],
    ctx: HydrationContext,
    session: AsyncSession,
) -> dict[str, Any]:
    """Claim-time hydrator for `ConfigUpdate` commands.

    Injects the current per-org API key secrets into `config.api_keys`.
    `org_id` arrives via the typed `HydrationContext` — no `_`-prefixed magic
    key in the payload dict.

    Credentials flow: `api_keys.get_all_for_org` → `SecretStr` values in the
    returned dict → unwrapped only at the wire-encode boundary by
    `AgentConfig.api_keys`' `@field_serializer(when_used="json")`.
    """
    from app.core.agent_gateway import get_api_key_secrets_provider  # noqa: PLC0415

    org_id: UUID = ctx.org_id
    out = dict(payload)

    provider = get_api_key_secrets_provider()
    if provider is None:
        return out

    api_keys = await provider(org_id, session=session)

    # Replace config.api_keys with the freshly-fetched secrets.
    config = dict(out.get("config") or {})
    config["api_keys"] = api_keys
    out["config"] = config
    return out


def _register_api_key_on_change() -> None:
    """Register the ConfigUpdate fan-out callback with api_keys at import time.

    In-function import keeps the `core/api_keys` edge out of the top-level namespace
    (tach `depends_on` lists top-level imports). The import is a module-level
    side-effect, executed once at first import of `core/coding_agent`.
    """
    import app.core.api_keys as _api_keys  # noqa: PLC0415

    _api_keys.register_on_change(_enqueue_config_update_for_all_org_agents)
