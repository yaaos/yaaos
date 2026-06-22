"""BYOK secrets provider and on-change wiring for coding-agent plugins.

`build_byok_secrets_for_org` is the byok-secrets-provider callable registered
with `core/agent_gateway` via the IoC seam. Called whenever a ConfigUpdate
is enqueued for an org so the agent receives fresh BYOK keys.

`_register_byok_on_change` registers the ConfigUpdate fan-out callback with
`core/byok` at module load time (called from `core/coding_agent/__init__.py`).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    enqueue_config_update_for_all_org_agents as _enqueue_config_update_for_all_org_agents,
)
from app.core.coding_agent.service import _get as _get_coding_agent_registry


async def build_byok_secrets_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> dict[str, SecretStr]:
    """Return a dict of provider_id → SecretStr for every registered plugin
    that has a byok_requirement and a stored key for the org.

    Called by `core/agent_gateway._build_config_update_dto` via the registered
    byok-secrets-provider IoC seam. Reads BYOK keys for all plugins that declare
    a `byok_requirement()`. Wraps each plaintext in SecretStr immediately so it
    never crosses module boundaries as a bare string.
    """
    import app.core.byok as _byok  # noqa: PLC0415

    result: dict[str, SecretStr] = {}
    for plugin in _get_coding_agent_registry().list():
        req = plugin.byok_requirement()
        if req is None:
            continue
        plaintext = await _byok.get(org_id, req, session=session)
        if plaintext is not None:
            result[req] = SecretStr(plaintext)
    return result


def _register_byok_on_change() -> None:
    """Register the ConfigUpdate fan-out callback with byok at import time.

    In-function import keeps the `core/byok` edge out of the top-level namespace
    (tach `depends_on` lists top-level imports). The import is a module-level
    side-effect, executed once at first import of `core/coding_agent`.
    """
    import app.core.byok as _byok  # noqa: PLC0415

    _byok.register_on_change(_enqueue_config_update_for_all_org_agents)
