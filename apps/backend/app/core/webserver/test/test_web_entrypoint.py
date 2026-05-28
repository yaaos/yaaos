"""Smoke test for the web composition root (`app/web.py`)."""

from __future__ import annotations

from app.web import app


def test_app_is_fastapi_instance() -> None:
    # fastapi.FastAPI is monkey-patched to _InstrumentedFastAPI by OTel after
    # the first import of app.web. Check via the module-origin check so the
    # assertion is immune to whether instrumentation has run before or after
    # this test in the collection order.
    assert type(app).__module__.startswith("fastapi") or type(app).__module__.startswith("opentelemetry"), (
        f"Expected a FastAPI (or OTel-instrumented FastAPI) app, got {type(app)}"
    )
    # Confirm the key FastAPI duck-type attributes are present.
    assert hasattr(app, "router")
    assert hasattr(app, "add_middleware")


def test_app_has_lifespan() -> None:
    # FastAPI stores the lifespan callable on router.lifespan_context when set.
    assert app.router.lifespan_context is not None
