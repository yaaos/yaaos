"""ASGI mounting for `domain/mcp_server`'s FastMCP sub-app.

`mount(app)` encapsulates everything the composition root needs to expose the
inbound MCP surface: the `/.well-known/oauth-authorization-server` discovery
route, the FastMCP Streamable-HTTP sub-app at `/api/mcp-server/mcp`, and the
lifespan chaining that keeps the sub-app's session manager alive.

Why the chaining exists: Starlette does NOT propagate lifespan events to
mounted sub-apps, so the FastMCP http_app's lifespan is chained into the
FastAPI router's `lifespan_context`. Without this the
`StreamableHTTPSessionManager`'s task group never initialises and every
`/api/mcp-server/mcp` request errors.

Why the proxy exists: `StreamableHTTPSessionManager.run()` can only be called
ONCE per instance. In tests that restart the ASGI lifespan (e.g. multiple
`TestClient()` uses), a fixed http_app would fail on the second lifespan
start. The proxy's inner handler is swapped to a FRESH `mcp.http_app()` on
each lifespan start, giving each startup a virgin session manager.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.domain.mcp_server.oauth_web import well_known_router
from app.domain.mcp_server.tools import mcp


class _MCPProxy:
    """ASGI proxy forwarding every call to the current per-lifespan http_app."""

    def __init__(self) -> None:
        self._inner: Any = None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if self._inner is None:
            raise RuntimeError("_MCPProxy not initialised — lifespan not running")
        await self._inner(scope, receive, send)


def mount(app: FastAPI) -> None:
    """Mount the inbound MCP server surface on `app`.

    Called by the composition root (`app/web.py`) AFTER `create_app()` so the
    FastAPI routes registered via RouteSpec (register, authorize,
    authorize/consent, token) appear in `app.routes` first and take priority
    over the sub-app Mount for their specific paths. Direct-mount mirrors the
    `e2e_setup.mount` pattern.
    """
    proxy = _MCPProxy()
    orig_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _combined_lifespan(_app):
        # Fresh instance every time so the session manager starts clean.
        mcp_http_app = mcp.http_app(path="/", stateless_http=True)
        proxy._inner = mcp_http_app
        try:
            async with orig_lifespan(_app):
                async with mcp_http_app.router.lifespan_context(mcp_http_app):
                    yield
        finally:
            proxy._inner = None

    app.router.lifespan_context = _combined_lifespan

    app.include_router(well_known_router, prefix="/.well-known", tags=["mcp_server"])
    app.mount("/api/mcp-server/mcp", proxy)
