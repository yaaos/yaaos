"""domain/mcp_server — inbound MCP authorization server + tool surface.

yaaos is an MCP server for local coding agents.  This module owns:
  - The OAuth AS surface (RFC 7591 registration, RFC 8414 discovery, PKCE-S256
    authorization code flow, opaque bearer tokens).
  - The FastMCP tool registry (one thin wrapper per domain service function).
  - Token lifecycle: mint, rotate, sweep, revoke.

FastMCP provides Streamable HTTP transport and bearer-auth middleware;
the AS endpoints are hand-rolled FastAPI routes because FastMCP's
OAuthProvider.authorize() hook has no access to the session cookie needed
for the browser-based consent flow.
"""

import app.domain.mcp_server.oauth_web  # noqa: F401 — registers /api/mcp-server routes
from app.domain.mcp_server.auth import (
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    McpAuthError,
    McpPrincipal,
    authenticate,
    revoke_tokens_for_user,
)
from app.domain.mcp_server.tools import mcp

__all__ = [
    "ACCESS_TOKEN_TTL",
    "REFRESH_TOKEN_TTL",
    "McpAuthError",
    "McpPrincipal",
    "authenticate",
    "mcp",
    "revoke_tokens_for_user",
]
