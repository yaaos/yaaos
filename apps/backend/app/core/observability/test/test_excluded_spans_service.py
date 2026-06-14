"""Service test — FastAPIInstrumentor `exclude_spans` suppresses `http send` and
`http receive` ASGI lifecycle child spans.

The FastAPI instrumentor's underlying ASGI middleware can emit two INTERNAL
child spans on every request span — one per ASGI lifecycle event:
  - "<method> <path> http receive"
  - "<method> <path> http send"

They carry no attributes and no children. The `exclude_spans=["send",
"receive"]` kwarg (available since OTel-contrib 0.49b0; pinned at 0.58b0)
suppresses them. This test asserts they are absent.

Isolation note: the test builds its own minimal FastAPI app and instruments it
directly via `instrument_app(exclude_spans=...)` so the result is independent
of whether the global `FastAPIInstrumentor().instrument()` has already run. It
then calls `uninstrument_app()` in the `finally` block so the global state is
unaffected.
"""

from __future__ import annotations

import pytest

pytest_plugins: list[str] = []


@pytest.mark.service
@pytest.mark.asyncio
async def test_fastapi_instrumentor_excludes_send_and_receive_lifecycle_spans() -> None:
    """No span whose name ends with `http send` or `http receive` is emitted."""
    import fastapi as _fastapi  # noqa: PLC0415
    import httpx  # noqa: PLC0415
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: PLC0415
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: PLC0415

    # Build an isolated TracerProvider + exporter so the test is independent
    # of the global provider state (configure() may or may not have run).
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Uninstrument globally first (if already instrumented) so we can
    # re-instrument with our local provider. Matches the pattern in
    # test_otel_export_service.py.
    global_inst = FastAPIInstrumentor()
    was_instrumented = global_inst._is_instrumented_by_opentelemetry
    if was_instrumented:
        global_inst.uninstrument()

    # Re-read FastAPI from the module to get the restored (non-instrumented)
    # class, not a cached `_InstrumentedFastAPI` reference.
    OrigFastAPI = _fastapi.FastAPI

    app = OrigFastAPI()

    @app.get("/probe")
    async def _probe() -> dict[str, str]:
        return {"ok": "true"}

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
            response = await ac.get("/probe")

        assert response.status_code == 200

        span_names = {s.name for s in exporter.get_finished_spans()}
        assert span_names, "Expected at least the SERVER span to be captured"

        for name in span_names:
            assert not name.endswith(" http send"), f"http send span leaked: {name!r}"
            assert not name.endswith(" http receive"), f"http receive span leaked: {name!r}"
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()
        # Restore global instrumentation state so other tests are unaffected.
        if was_instrumented:
            global_inst.instrument()
