"""domain/mcp_proxy — per-review MCP bearer + proxy."""

from app.domain.mcp_proxy.service import (
    REVIEW_TOKEN_TTL,
    McpToken,
    consume_broken_creds,
    get_token_by_hash,
    hash_token,
    lookup_token,
    mint_token,
    record_broken_creds,
    revoke_token,
)

__all__ = [
    "REVIEW_TOKEN_TTL",
    "McpToken",
    "consume_broken_creds",
    "get_token_by_hash",
    "hash_token",
    "lookup_token",
    "mint_token",
    "record_broken_creds",
    "revoke_token",
]

# Side-effect import: registers /api/mcp/* routes. Not in __all__ (Rule-9).
import app.domain.mcp_proxy.web  # noqa: F401
