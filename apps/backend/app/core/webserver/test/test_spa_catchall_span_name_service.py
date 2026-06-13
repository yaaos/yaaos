"""Service test — SPA catch-all handler rewrites its OTel span name.

The catch-all route (`/{full_path:path}`) normally produces a span named
`GET /{full_path:path}`, collapsing every SPA page-load and static asset into
one trace bucket. The handler calls `_rename_catchall_span` which rewrites the
active span name to `{method} {path-up-to-3-segments}` so distinct SPA routes
are distinguishable in trace UIs.

Isolation note: the test builds a minimal FastAPI app with `_install_spa_serving`
mounted against a stub dist tree (`tmp_path`), instruments it with its own
isolated TracerProvider, and asserts on the SERVER span names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins: list[str] = []


@pytest.mark.service
@pytest.mark.asyncio
async def test_spa_catchall_span_name_uses_actual_path(tmp_path: Path) -> None:
    """Span name is bucketed to first 3 path segments, not `/{full_path:path}`."""
    import fastapi as _fastapi  # noqa: PLC0415
    import httpx  # noqa: PLC0415
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: PLC0415
    from opentelemetry.trace import SpanKind  # noqa: PLC0415

    from app.core.webserver.app_factory import _install_spa_serving  # noqa: PLC0415

    # Build a stub dist tree under tmp_path so _install_spa_serving mounts the
    # catch-all without requiring a real `pnpm build` output.
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "logos").mkdir()
    (dist / "logos" / "x.svg").write_text("<svg/>")

    # Isolated TracerProvider so the test is independent of global OTel state.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Uninstrument globally first (if already instrumented) so we can
    # re-instrument with our local provider. Matches the pattern in
    # test_excluded_spans_service.py.
    global_inst = FastAPIInstrumentor()
    was_instrumented = global_inst._is_instrumented_by_opentelemetry
    if was_instrumented:
        global_inst.uninstrument()

    # Create the FastAPI app AFTER uninstrument() so we get the original
    # (non-instrumented) class, not a cached `_InstrumentedFastAPI` reference.
    OrigFastAPI = _fastapi.FastAPI
    app = OrigFastAPI()
    _install_spa_serving(app, dist_path=dist)

    try:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            exclude_spans=["send", "receive"],
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            await ac.get("/logos/x.svg")
            await ac.get("/org/foo/tickets/abc-def-123")
            await ac.get("/")

        server_spans = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
        names = {s.name for s in server_spans}

        # Static file with 2 segments — kept as-is (under 3-segment cap).
        assert "GET /logos/x.svg" in names, f"Expected 'GET /logos/x.svg' in {names}"
        # SPA route with 4 segments — 4th segment (UUID) truncated to 3.
        assert "GET /org/foo/tickets" in names, f"Expected 'GET /org/foo/tickets' in {names}"
        # Root path produces '/'.
        assert "GET /" in names, f"Expected 'GET /' in {names}"
        # The raw catch-all template must NOT appear.
        assert "GET /{full_path:path}" not in names, f"Template span leaked into {names}"
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()
        if was_instrumented:
            global_inst.instrument()
