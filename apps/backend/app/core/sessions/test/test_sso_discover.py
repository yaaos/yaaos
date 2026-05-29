"""Verify GET /api/auth/sso/discover is no longer served by core/sessions.

The discover endpoint moved to domain/orgs: GET /api/sso/discover.
These tests assert the old auth-prefix route is gone so the SPA migration
from /api/auth/sso/discover → /api/sso/discover is not silently reverted.

Full positive coverage of the discover behavior lives in
`apps/backend/app/domain/orgs/test/test_sso_discover_service.py`.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.sessions import web as _sessions_web  # noqa: F401


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"sessions"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_sso_discover_not_on_auth_prefix() -> None:
    """`/api/auth/sso/discover` must return 404 — the route was removed from
    `core/sessions` and lives at `/api/sso/discover` in `domain/orgs`."""
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "user@example.com"})
    assert r.status_code == 404, f"Route should be gone from core/sessions; got {r.status_code}: {r.text}"
