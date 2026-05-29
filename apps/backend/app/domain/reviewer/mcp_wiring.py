"""MCP-side wiring for the reviewer pipeline.

Two pieces, shared by the workflow-engine path and tests:

- `build_mcp_payload(review_id, org_id)` — collect connected MCP
  providers for the org, mint a per-review bearer, and return the
  `agent_config["mcp"]` payload that `plugins/claude_code` materializes
  into a workspace `.mcp.json`. Returns None when nothing's connected.
- `prefix_broken_creds_warning(body, providers)` — prepend a yellow
  GitHub callout to the PR-review summary listing any providers that
  returned `broken_creds` / `not_connected` during the review. No-op
  when the list is empty.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.database import session as db_session
from app.domain import (
    integrations as mcp_integrations,
)
from app.domain import (
    mcp_proxy,
)

log = structlog.get_logger("reviewer.mcp_wiring")


async def build_mcp_payload(review_id: UUID, *, org_id: UUID) -> dict[str, Any] | None:
    """Collect connected MCP providers for the org and mint a per-review bearer.

    Returns None when no providers are connected (or all are broken/disabled) —
    the reviewer still runs, just without MCP context. The bearer + provider
    catalogue are threaded into the agent via `ReviewContext.agent_config["mcp"]`;
    `plugins/claude_code` materializes the workspace `.mcp.json` from it.
    """
    servers: list[dict[str, Any]] = []
    async with db_session() as s:
        for provider_id in mcp_integrations.known_providers():
            row = await mcp_integrations.get(s, org_id, provider_id)
            if row is None or not row.enabled:
                continue
            if row.last_refresh_status == "failed":
                log.warning(
                    "review.mcp.broken_creds_skipped",
                    provider=provider_id,
                    org_id=str(org_id),
                )
                continue
            prov = mcp_integrations.get_provider(provider_id)
            servers.append(
                {
                    "provider": provider_id,
                    "allowed_tools": list(row.allowed_tools),
                    "known_read_tools": list(prov.config.known_read_tools) if prov else [],
                    "known_write_tools": list(prov.config.known_write_tools) if prov else [],
                }
            )
    if not servers:
        log.info("review.mcp.no_connected_providers", org_id=str(org_id))
        return None
    async with db_session() as s:
        raw_token = await mcp_proxy.mint_token(review_id, org_id=org_id, session=s)
        await s.commit()
    return {
        "token": raw_token,
        "base_url": f"{get_settings().yaaos_app_base_url}/api/mcp/{review_id}",
        "servers": servers,
    }


def prefix_broken_creds_warning(body: str | None, providers: list[str]) -> str | None:
    """Prefix the PR review summary with a yellow GitHub callout listing any
    MCP providers that returned `broken_creds`/`not_connected` during this
    review. No-op when nothing was observed."""
    if not providers:
        return body
    names = ", ".join(providers)
    note = (
        "> [!WARNING]\n"
        f"> The following MCP integrations returned errors during this review "
        f"and were skipped: **{names}**. Reconnect them in Org Settings → Integrations.\n"
    )
    if not body:
        return note
    return f"{note}\n{body}"


__all__ = ["build_mcp_payload", "prefix_broken_creds_warning"]
