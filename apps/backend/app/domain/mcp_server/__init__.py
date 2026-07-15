"""domain/mcp_server — inbound MCP authorization server + tool surface.

yaaos is an MCP server for local coding agents.  This module owns:
  - The OAuth AS surface (RFC 7591 registration, RFC 8414 discovery, PKCE-S256
    authorization code flow, opaque bearer tokens).
  - The FastMCP tool registry (one thin wrapper per domain service function).
  - Token lifecycle: mint, rotate, sweep, revoke.
  - `mount(app)` — mounts the FastMCP ASGI sub-app + discovery route on the
    composition root's FastAPI app (see `asgi.py`).

FastMCP provides Streamable HTTP transport and bearer-auth middleware;
the AS endpoints are hand-rolled FastAPI routes because FastMCP's
OAuthProvider.authorize() hook has no access to the session cookie needed
for the browser-based consent flow.
"""

import app.domain.mcp_server.oauth_web  # noqa: F401 — registers /api/mcp-server routes
from app.core.identity import register_user_deletion_hook as _register_user_deletion_hook
from app.domain.mcp_server.asgi import mount
from app.domain.mcp_server.auth import (
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    McpAuthError,
    McpPrincipal,
    authenticate,
    revoke_tokens_for_user,
)

__all__ = [
    "ACCESS_TOKEN_TTL",
    "REFRESH_TOKEN_TTL",
    "McpAuthError",
    "McpPrincipal",
    "authenticate",
    "mount",
    "revoke_tokens_for_user",
]

# MCP bearers must not outlive the user row — the token tables carry no FK to
# `users`, so `core/identity.delete_user` invokes this hook in its transaction.
_register_user_deletion_hook(revoke_tokens_for_user)
