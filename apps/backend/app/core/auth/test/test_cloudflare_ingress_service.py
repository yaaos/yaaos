"""Service tests for CloudflareIngressMiddleware.

Scenarios:
1. Secret configured + no header → 403 (outermost gate, not a deeper-layer 4xx).
2. Secret configured + correct header → request passes to handler.
3. /api/health + no header → reaches the health handler (exempt path).
4. Empty secret Setting → middleware is a no-op (normal route passes, health passes).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.core.auth.cloudflare import CLOUDFLARE_INGRESS_HEADER, CloudflareIngressMiddleware


@contextmanager
def _ingress_secret(value: str) -> Iterator[FastAPI]:
    """Context manager that sets YAAOS_CLOUDFLARE_INGRESS_SECRET, builds a
    minimal test app, and restores the prior env state on exit."""
    prior = os.environ.get("YAAOS_CLOUDFLARE_INGRESS_SECRET")
    os.environ["YAAOS_CLOUDFLARE_INGRESS_SECRET"] = value

    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(CloudflareIngressMiddleware)

    @app.get("/api/some-route")
    async def _probe(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/api/health")
    async def _health(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    try:
        yield app
    finally:
        if prior is None:
            os.environ.pop("YAAOS_CLOUDFLARE_INGRESS_SECRET", None)
        else:
            os.environ["YAAOS_CLOUDFLARE_INGRESS_SECRET"] = prior
        get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.service
async def test_request_without_header_gets_403_when_secret_set() -> None:
    """A request to a normal route with no CF-Access header → 403.

    Proves outermost ordering: the middleware fires before any deeper-layer
    auth, so this is a 403 from the ingress gate, not a 200/4xx from a
    route handler or AuthMiddleware.
    """
    with _ingress_secret("my-secret-value") as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/some-route")
    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_request_with_correct_header_passes() -> None:
    """A request carrying the correct shared-secret header passes to the handler."""
    with _ingress_secret("my-secret-value") as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/some-route",
                headers={CLOUDFLARE_INGRESS_HEADER: "my-secret-value"},
            )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.service
async def test_health_path_exempt_from_ingress_check() -> None:
    """/api/health is exempt: Fly's internal checker bypasses Cloudflare."""
    with _ingress_secret("some-secret") as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_middleware_is_noop_when_secret_empty_normal_route() -> None:
    """With an empty secret Setting, the middleware is a no-op.

    A normal route without the CF header passes through to the handler.
    """
    with _ingress_secret("") as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/some-route")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.service
async def test_middleware_is_noop_when_secret_empty_health_route() -> None:
    """With an empty secret Setting, /api/health is also a pass-through."""
    with _ingress_secret("") as app:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
