"""domain/mcp_proxy — per-review MCP bearer + proxy.

Skeleton at Phase 0. Phase 2 ships `mint_token`, `revoke_token`,
`dispatch`, and the FastAPI Streamable-HTTP router.
"""

from app.domain.mcp_proxy.models import McpReviewTokenRow

__all__ = ["McpReviewTokenRow"]
