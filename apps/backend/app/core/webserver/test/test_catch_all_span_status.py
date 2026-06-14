"""Service test: FastAPI catch-all exception handler records span exception.

When a route raises an unhandled exception the catch-all handler at
`app_factory.py:_unhandled` must call
`span.record_exception(exc)` + `span.set_status(ERROR, ...)` on the
active request span before returning the 500 JSON response.

This test drives the catch-all logic directly (without the full `create_app()`
stack) via a minimal FastAPI app instrumented with the OTel FastAPI
instrumentor. The handler logic matches the implementation in `_install_middleware`
exactly — both call `trace.get_current_span().record_exception(exc)` +
`set_status(ERROR, "internal_server_error")`.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from starlette.requests import Request

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


@pytest.fixture
def _raising_app() -> FastAPI:
    """Minimal FastAPI app that mirrors the yaaos catch-all handler and has
    one route that always raises. Instrumented with OTel so a request span
    is created around each request."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    app = FastAPI()

    # Mirror the exact catch-all body from _install_middleware in app_factory.py.
    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        trace.get_current_span().record_exception(exc)
        trace.get_current_span().set_status(StatusCode.ERROR, "internal_server_error")
        logging.getLogger("yaaos").exception("http.unhandled_exception", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("intentional failure")

    # Instrument the app so a request span wraps each route call.
    FastAPIInstrumentor().instrument_app(app)
    return app


async def test_fastapi_catchall_records_exception_on_request_span(_raising_app) -> None:
    """GET /boom raises → request span carries `exception` event and
    `StatusCode.ERROR`; response body is `{"error": "internal_server_error"}`."""
    with span_capture() as exporter:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_raising_app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/boom")

    assert resp.status_code == 500, f"expected 500, got {resp.status_code}"
    assert resp.json() == {"error": "internal_server_error"}

    spans = exporter.get_finished_spans()

    # Find the main HTTP server span for the failing request.
    # FastAPI instrumentation names it "GET /boom" (method + route).
    # Exclude sub-spans like "GET /boom http send" (those are ASGI lifecycle spans
    # without exception events).
    request_spans = [
        s
        for s in spans
        if s.name == "GET /boom"
        or ("boom" in s.name and "http send" not in s.name and "http receive" not in s.name)
    ]
    assert request_spans, (
        f"expected a request span for /boom, got: {[(s.name, dict(s.attributes or {})) for s in spans]}"
    )
    req_span = request_spans[0]

    assert req_span.status.status_code == StatusCode.ERROR, (
        f"request span expected ERROR, got {req_span.status.status_code}"
    )
    exception_events = [e for e in req_span.events if e.name == "exception"]
    assert exception_events, (
        f"expected an 'exception' event on the request span, events: {[e.name for e in req_span.events]}"
    )
