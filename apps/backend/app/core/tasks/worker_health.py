"""Minimal worker health server — single-route Starlette app.

Exposes `GET /health` returning 200 when:
  - `database.ping()` succeeds
  - `redis.ping()` succeeds
  - the liveness heartbeat is fresh (last_tick within the stale threshold)

Returns 503 when any condition fails.  The body shape mirrors
`core/webserver/health.py` with an added `heartbeat_ok` field.

The server is NOT the main FastAPI app — it uses a dedicated minimal
Starlette ASGI app run via a background `uvicorn.Server` task inside
`runtime.run()`, bound to `0.0.0.0` so Fly's machine checker reaches it
directly without going through Cloudflare (which would 403 the probe).

`WorkerHeartbeat` tracks the last time the liveness ticker fired.  The ticker
is a small asyncio task running every `_TICKER_INTERVAL_SECONDS` inside
`runtime.run()`.  The health handler checks that
`now - last_tick < stale_threshold_seconds`; a wedged consume loop stops
advancing the ticker, making the check go 503 within two missed ticks.

Production usage::

    heartbeat = WorkerHeartbeat()
    health_app = build_worker_health_app(heartbeat=heartbeat)
    # ... pass health_app to a uvicorn.Server config ...

Test usage::

    heartbeat = WorkerHeartbeat(stale_threshold_seconds=30.0)
    heartbeat.tick()
    app = build_worker_health_app(
        heartbeat=heartbeat,
        db_ping=lambda: ...,
        redis_ping=lambda: ...,
    )
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import app.core.database as database
import app.core.redis as redis_client

# How long (seconds) the health ticker is allowed to go without a tick before
# the heartbeat is considered stale.  Two ticker intervals (10 s) gives one
# missed beat before alarming — generous enough to survive GC pauses, tight
# enough to catch a wedged loop within ~20 s.
_STALE_THRESHOLD_SECONDS: float = 60.0

# How often the liveness ticker fires in runtime.run().
TICKER_INTERVAL_SECONDS: float = 5.0


class WorkerHeartbeat:
    """Tracks the last time the liveness ticker fired.

    Starts with `_last_tick = 0.0` (epoch) so `is_fresh()` returns False
    until the first `tick()` — intentional; the health check stays 503
    until the worker loop is confirmed running.
    """

    def __init__(self, stale_threshold_seconds: float = _STALE_THRESHOLD_SECONDS) -> None:
        self._last_tick: float = 0.0
        self._stale_threshold = stale_threshold_seconds

    def tick(self) -> None:
        """Record a liveness heartbeat at the current monotonic time."""
        self._last_tick = time.monotonic()

    def is_fresh(self) -> bool:
        """Return True when the last tick is within the stale threshold."""
        if self._last_tick == 0.0:
            return False
        return (time.monotonic() - self._last_tick) < self._stale_threshold


def build_worker_health_app(
    *,
    heartbeat: WorkerHeartbeat,
    db_ping: Callable[[], Awaitable[bool]] | None = None,
    redis_ping: Callable[[], Awaitable[bool]] | None = None,
) -> Starlette:
    """Return a minimal single-route Starlette ASGI app for the worker health check.

    The `db_ping` and `redis_ping` callables default to the real ping helpers
    from `core/database` and `core/redis` so production callers pass only
    `heartbeat=`.  Tests inject lightweight stubs to avoid real DB/Redis.
    """
    _db_ping: Callable[[], Awaitable[bool]] = db_ping if db_ping is not None else database.ping
    _redis_ping: Callable[[], Awaitable[bool]] = redis_ping if redis_ping is not None else redis_client.ping

    async def health(request: Request) -> JSONResponse:
        db_ok, redis_ok = await _db_ping(), await _redis_ping()
        heartbeat_ok = heartbeat.is_fresh()
        healthy = db_ok and redis_ok and heartbeat_ok
        body = {
            "status": "ok" if healthy else "degraded",
            "db_ok": db_ok,
            "redis_ok": redis_ok,
            "heartbeat_ok": heartbeat_ok,
        }
        return JSONResponse(content=body, status_code=200 if healthy else 503)

    return Starlette(routes=[Route("/health", health)])
