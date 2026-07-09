"""API key secrets provider and on-change wiring for coding-agent plugins.

`build_api_key_secrets_for_org` is the api-key-secrets-provider callable registered
with `core/agent_gateway` via the IoC seam. Called whenever a ConfigUpdate
is enqueued for an org so the agent receives fresh API keys.

`_register_api_key_on_change` registers the ConfigUpdate fan-out callback with
`core/api_keys` at module load time (called from `core/coding_agent/__init__.py`).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

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
    unknown providers are ignored there by design. Wraps each plaintext in
    SecretStr immediately so it never crosses module boundaries as a bare string.

    Called by `core/agent_gateway._build_config_update_dto` via the registered
    api-key-secrets-provider IoC seam.
    """
    import app.core.api_keys as _api_keys  # noqa: PLC0415

    stored_keys = await _api_keys.list_keys_for_org(org_id, session=session)
    result: dict[str, SecretStr] = {}
    for api_key in stored_keys:
        plaintext = await _api_keys.get(org_id, api_key.provider, session=session)
        if plaintext is not None:
            result[api_key.provider] = SecretStr(plaintext)
    return result


def _register_api_key_on_change() -> None:
    """Register the ConfigUpdate fan-out callback with api_keys at import time.

    In-function import keeps the `core/api_keys` edge out of the top-level namespace
    (tach `depends_on` lists top-level imports). The import is a module-level
    side-effect, executed once at first import of `core/coding_agent`.
    """
    import app.core.api_keys as _api_keys  # noqa: PLC0415

    _api_keys.register_on_change(_enqueue_config_update_for_all_org_agents)
