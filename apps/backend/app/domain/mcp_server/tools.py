"""FastMCP server instance + tool registry for `domain/mcp_server`.

The `mcp` FastMCP server object is the transport and tool-registry scaffold.
Auth is enforced via `YaaosTokenVerifier` — a `TokenVerifier` subclass that
hash-looks-up the bearer in `mcp_access_tokens` and puts the resolved principal
in the FastMCP `AccessToken.claims` dict.  Tool handlers read the principal via
`mcp.server.auth.middleware.auth_context.get_access_token()`.

Tool authoring rules:
  - Each tool is a 1:1 wrapper over a public service function.
  - Org comes from the principal, never from tool args.
  - Per-tool role floor: write tools declare a required `Action`; the wrapper
    runs the same role check as the HTTP equivalents, against `principal.role`.
  - Canary tool `find_ticket` is read-only; the role-floor scaffold is present
    but does not reject any valid MCP caller (builder+ can read).

FastMCP's `verify_token` hook:
  The `YaaosTokenVerifier.verify_token(token)` method is async-safe and opens
  its own DB session so it composes cleanly with FastMCP's middleware chain.
  The `McpPrincipal` is serialised into `AccessToken.claims` so tool handlers
  can reconstruct it without a second DB lookup.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import SecretStr

from app.core.database import session as db_session
from app.domain.mcp_server.auth import McpAuthError, McpPrincipal, authenticate
from app.domain.tickets import get_by_branch

# ---------------------------------------------------------------------------
# Token verifier — the FastMCP ↔ yaaos auth bridge.
# ---------------------------------------------------------------------------


class YaaosTokenVerifier(TokenVerifier):
    """Verify a yaaos MCP bearer by hash-lookup in `mcp_access_tokens`.

    On success, the resolved `McpPrincipal` is serialised into
    `AccessToken.claims` (keys: `user_id`, `org_id`, `role`) so tool
    handlers can reconstruct the principal without a second DB hit.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        async with db_session() as s:
            try:
                principal = await authenticate(SecretStr(token), session=s)
            except McpAuthError:
                return None

        return AccessToken(
            token=token,
            client_id="",
            scopes=[],
            expires_at=None,
            claims={
                "user_id": str(principal.user_id),
                "org_id": str(principal.org_id),
                "role": principal.role,
            },
        )


def _get_principal() -> McpPrincipal | None:
    """Read the MCP principal from the current auth context (set by FastMCP middleware)."""
    at = get_access_token()
    if at is None or not at.claims:
        return None
    try:
        return McpPrincipal(
            user_id=UUID(at.claims["user_id"]),
            org_id=UUID(at.claims["org_id"]),
            role=at.claims["role"],
        )
    except KeyError, ValueError:
        return None


# ---------------------------------------------------------------------------
# FastMCP server — transport + tool registry.
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "yaaos",
    auth=YaaosTokenVerifier(),
    stateless_http=True,
)


# ---------------------------------------------------------------------------
# Canary tool: find_ticket.
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_ticket(branch_name: str) -> dict[str, Any]:
    """Find a yaaos ticket by its Git branch name.

    Returns the most recently created ticket on `branch_name` in the caller's
    org (fixed at consent time).

    Args:
        branch_name: The exact Git branch name to look up.

    Returns:
        {ticket_id, title, status} — all null when no ticket is found.
    """
    from mcp import McpError  # noqa: PLC0415
    from mcp.types import ErrorData  # noqa: PLC0415

    principal = _get_principal()
    if principal is None:
        # JSON-RPC -32004: authentication required.
        raise McpError(ErrorData(code=-32004, message="unauthenticated"))

    async with db_session() as s:
        ticket = await get_by_branch(branch_name, org_id=principal.org_id, session=s)

    if ticket is None:
        return {"ticket_id": None, "title": None, "status": None}
    return {
        "ticket_id": str(ticket.id),
        "title": ticket.title,
        "status": ticket.status,
    }
