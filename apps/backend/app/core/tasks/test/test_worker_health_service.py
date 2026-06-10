"""Service tests: worker health server returns correct status codes.

Covers three contracts:
  - 200 when database ping + redis ping pass and the heartbeat is fresh.
  - 503 when the database ping fails.
  - 503 when the heartbeat timestamp is stale (liveness ticker stopped).

The health handler is tested directly — no real HTTP server is started.
`_worker_health_handler` is the ASGI callable; we exercise it via
`httpx.ASGITransport` with a fake `_WorkerHeartbeat` that can simulate each
condition.
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.core.tasks.worker_health import WorkerHeartbeat, build_worker_health_app


@pytest.mark.asyncio
@pytest.mark.service
async def test_health_returns_200_when_pings_pass_and_heartbeat_fresh() -> None:
    """200 with status=ok when DB + Redis pass and last_tick is recent."""
    heartbeat = WorkerHeartbeat(stale_threshold_seconds=30.0)
    heartbeat.tick()  # mark fresh

    app = build_worker_health_app(
        heartbeat=heartbeat,
        db_ping=lambda: _async_ok(True),
        redis_ping=lambda: _async_ok(True),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["redis_ok"] is True
    assert body["heartbeat_ok"] is True


@pytest.mark.asyncio
@pytest.mark.service
async def test_health_returns_503_when_db_ping_fails() -> None:
    """503 with status=degraded when DB ping returns False."""
    heartbeat = WorkerHeartbeat(stale_threshold_seconds=30.0)
    heartbeat.tick()

    app = build_worker_health_app(
        heartbeat=heartbeat,
        db_ping=lambda: _async_ok(False),
        redis_ping=lambda: _async_ok(True),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db_ok"] is False
    assert body["redis_ok"] is True
    assert body["heartbeat_ok"] is True


@pytest.mark.asyncio
@pytest.mark.service
async def test_health_returns_503_when_redis_ping_fails() -> None:
    """503 with status=degraded when Redis ping returns False."""
    heartbeat = WorkerHeartbeat(stale_threshold_seconds=30.0)
    heartbeat.tick()

    app = build_worker_health_app(
        heartbeat=heartbeat,
        db_ping=lambda: _async_ok(True),
        redis_ping=lambda: _async_ok(False),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db_ok"] is True
    assert body["redis_ok"] is False
    assert body["heartbeat_ok"] is True


@pytest.mark.asyncio
@pytest.mark.service
async def test_health_returns_503_when_heartbeat_stale() -> None:
    """503 with status=degraded when last_tick is older than the stale threshold."""
    # Use a very short threshold so we don't need to sleep.
    heartbeat = WorkerHeartbeat(stale_threshold_seconds=0.0)
    # Do NOT call heartbeat.tick() — last_tick remains at epoch (0.0),
    # so now - last_tick >> 0.0.

    app = build_worker_health_app(
        heartbeat=heartbeat,
        db_ping=lambda: _async_ok(True),
        redis_ping=lambda: _async_ok(True),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db_ok"] is True
    assert body["redis_ok"] is True
    assert body["heartbeat_ok"] is False


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_freshness_after_tick() -> None:
    """WorkerHeartbeat.is_fresh() returns True immediately after tick()."""
    heartbeat = WorkerHeartbeat(stale_threshold_seconds=30.0)
    assert not heartbeat.is_fresh(), "should start stale (never ticked)"

    heartbeat.tick()
    assert heartbeat.is_fresh(), "should be fresh right after tick()"


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_goes_stale() -> None:
    """WorkerHeartbeat.is_fresh() returns False once the threshold is exceeded."""
    # Record a tick in the past by overriding _last_tick directly.
    heartbeat = WorkerHeartbeat(stale_threshold_seconds=1.0)
    heartbeat.tick()
    # Wind the clock back by more than the threshold.
    heartbeat._last_tick = time.monotonic() - 5.0
    assert not heartbeat.is_fresh(), "should be stale after threshold passed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_ok(value: bool) -> bool:
    return value
