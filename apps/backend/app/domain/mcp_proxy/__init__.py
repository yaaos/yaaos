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

# NOTE: `mcp_proxy.web` is not imported here to avoid potential circular imports.
# It appears in `__all__` so tach allows side-effect imports from other modules.

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
    "web",
]
