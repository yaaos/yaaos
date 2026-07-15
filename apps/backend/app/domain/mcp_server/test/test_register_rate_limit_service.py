"""Service-tier tests for the `POST /api/mcp-server/register` rate limiter.

Two sliding windows on the same source-IP axis: a burst window and a sustained
window. Both must pass for a registration to be accepted. The sustained test
clears the burst window between calls (simulating the passage of a minute) so
the 11th call's rejection is attributable to the sustained window alone.

HTTP routes exercised via `httpx.ASGITransport` (in-process, no network); each
test picks a unique source IP so windows never collide across tests.
"""

from __future__ import annotations

import itertools

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.redis import delete_keys_with_prefix
from app.core.webserver import mount_specs
from app.domain.mcp_server.rate_limit import (
    BURST_LIMIT,
    BURST_WINDOW_SECONDS,
    SUSTAINED_LIMIT,
    SUSTAINED_WINDOW_SECONDS,
    _burst_key,
    check_register,
)

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_ENDPOINT = "/api/mcp-server/register"

# Each _unique_ip() call allocates one address from 10.0.0.0/8 so no test
# shares a rate-limit window with another.
_ip_counter = itertools.count(1)


def _unique_ip() -> str:
    n = next(_ip_counter)
    return f"10.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}"


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"mcp_server"})
    return app


async def _register(ip: str) -> httpx.Response:
    """One registration attempt from `ip`. Valid metadata — only the limiter can reject it."""
    transport = httpx.ASGITransport(app=_app(), client=(ip, 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(
            _ENDPOINT,
            json={"client_name": "rl-client", "redirect_uris": ["https://example.com/cb"]},
        )


async def _clear_burst(ip: str) -> None:
    """Drop the burst window for `ip` — stands in for a minute elapsing."""
    await delete_keys_with_prefix(_burst_key(ip))


@pytest.mark.asyncio
async def test_burst_limit_trips_on_fourth_registration() -> None:
    ip = _unique_ip()
    for _ in range(BURST_LIMIT):
        assert (await _register(ip)).status_code == 201

    resp = await _register(ip)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == str(BURST_WINDOW_SECONDS)
    assert resp.json()["error"] == "too_many_requests"


@pytest.mark.asyncio
async def test_sustained_limit_trips_on_eleventh_registration() -> None:
    ip = _unique_ip()
    for _ in range(SUSTAINED_LIMIT):
        await _clear_burst(ip)
        assert (await _register(ip)).status_code == 201

    await _clear_burst(ip)
    resp = await _register(ip)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == str(SUSTAINED_WINDOW_SECONDS)
    assert resp.json()["error"] == "too_many_requests"


@pytest.mark.asyncio
async def test_other_ip_is_unaffected() -> None:
    throttled = _unique_ip()
    for _ in range(BURST_LIMIT):
        assert (await _register(throttled)).status_code == 201
    assert (await _register(throttled)).status_code == 429

    assert (await _register(_unique_ip())).status_code == 201


@pytest.mark.asyncio
async def test_none_source_ip_skips_check() -> None:
    """No-op when the request carries no client (proxy stripping / test harness)."""
    for _ in range(BURST_LIMIT + SUSTAINED_LIMIT + 1):
        await check_register(source_ip=None)
