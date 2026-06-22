"""Testing endpoint mount helpers — prod safety guards for /api/testing/* routes.

`mount_testing_endpoints` is the production-safety gate for the /api/testing/*
surface. It raises `RuntimeError` immediately when called with a production
settings instance, preventing testing endpoints from ever being mounted in prod.
The actual `e2e_setup.mount(app)` call is made by the composition root (app/web.py)
after this guard — core/webserver cannot import from app.testing (core < testing
in the layer hierarchy).

`assert_no_testing_routes_in_prod` is a defense-in-depth canary that iterates
the app's route list and raises if any path starts with /api/testing/ while in
production mode. It is a no-op in non-production environments.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import Settings


def mount_testing_endpoints(app: FastAPI, settings: Settings) -> None:
    """Production-safety gate for /api/testing/* routes.

    Raises `RuntimeError` when `settings.is_production` is True — testing
    endpoints must never be active in a production instance.

    In non-production the function is a no-op; the composition root (app/web.py)
    is responsible for calling `e2e_setup.mount(app)` after this guard succeeds.
    This separation keeps `core/webserver` free of `app.testing` imports (layer
    order: core < testing).
    """
    if settings.is_production:
        raise RuntimeError(
            "mount_testing_endpoints cannot be called in production; "
            "testing endpoints must never be active in prod"
        )


def assert_no_testing_routes_in_prod(app: FastAPI, settings: Settings) -> None:
    """Assert that no /api/testing/* routes exist on the app in production.

    No-op when `not settings.is_production`. In production, iterates
    `app.routes` and raises `RuntimeError` if any route path starts with
    `/api/testing/` — a defense-in-depth check that catches accidental
    testing-route leakage in production builds.
    """
    if not settings.is_production:
        return
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/testing/"):
            raise RuntimeError(
                f"Testing route {path!r} found in production app — "
                "testing endpoints must never be active in prod"
            )
