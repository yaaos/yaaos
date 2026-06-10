"""Rate-limit primitives backed by `slowapi`.

The `Limiter` instance is exported at module scope so individual endpoints
can decorate themselves (`@limiter.limit("30/minute")`). `app_factory`
wires the same instance into `app.state.limiter` + the exception handler.

In `test`, `_enabled` is False, the limiter has no storage backend, and
decorated endpoints behave as no-ops — keeps the suite fast.

Limits applied today:

- `/api/auth/*` anonymous endpoints — **30/minute per IP** to catch
  credential-stuffing + replay.
- Mutating endpoints (`POST`/`PUT`/`PATCH`/`DELETE`) keyed by session
  cookie — **120/minute** to catch runaway clients without throttling
  legitimate UI use.
"""

from __future__ import annotations

from starlette.requests import Request

from app.core.config import get_settings


def _per_user_key(request: Request) -> str:
    from slowapi.util import get_remote_address  # noqa: PLC0415

    cookie = request.cookies.get("yaaos_session", "")
    return f"u:{cookie}" if cookie else f"ip:{get_remote_address(request)}"


def _enabled() -> bool:
    """Enable rate limiting only in `production`. `dev`/`test` skip it so
    Playwright suites + ad-hoc local testing aren't throttled."""
    return get_settings().is_production


# Lazy slowapi import — avoids the dep when tests skip it.
try:
    from slowapi import Limiter

    limiter = Limiter(key_func=_per_user_key, default_limits=[], enabled=_enabled())
except Exception:  # pragma: no cover — slowapi is in pyproject
    limiter = None  # type: ignore[assignment]


# Convenience decorators. Routes use these as `@AUTH_LIMIT` / `@MUTATE_LIMIT`
# so the rate strings live in one place.
AUTH_LIMIT = "30/minute"
MUTATE_LIMIT = "120/minute"


__all__ = ["AUTH_LIMIT", "MUTATE_LIMIT", "limiter"]
