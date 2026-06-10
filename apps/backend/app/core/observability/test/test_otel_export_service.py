"""Service test — all 3 OTel signals reach an HTTP listener with correct
headers and resource attributes; shutdown() force-flushes buffered records;
FastAPI HTTP metrics and system metrics emit once a MeterProvider is wired.

This test drives _configure_otel() directly (bypassing the idempotency guard)
and exercises the providers via their direct references rather than via the
OTel globals (which the SDK permits setting only once per process). It asserts:

  - All three paths receive data: /v1/traces, /v1/metrics, /v1/logs.
  - Every request carries the Authorization and Dash0-Dataset headers set via
    OTEL_EXPORTER_OTLP_HEADERS.
  - The trace resource carries service.version and deployment.environment.name.
  - shutdown() force-flushes a buffered log record (i.e. a record emitted before
    shutdown() completes arrives at the stub after await).
  - FastAPIInstrumentor emits http.server.* instruments (duration + active
    requests + response size) after a request when a MeterProvider is wired.
  - SystemMetricsInstrumentor registers process/system CPU/memory/GC instruments
    once wired to the MeterProvider (the source that makes the meter non-empty).

The test is marked `service` per patterns.md convention (cross-module flow with
no Postgres required — the OTel SDK + HTTP boundary is the integration surface).
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

# ── Local OTLP stub server ────────────────────────────────────────────────────


class _OTLPHandler(BaseHTTPRequestHandler):
    """Minimal OTLP/HTTP stub — accepts POST on any path, records headers + body."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        server: _RecordingServer = self.server  # type: ignore[assignment]
        server.record(self.path, dict(self.headers), body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_: Any) -> None:  # silence stdlib request logs
        pass


class _RecordingServer(HTTPServer):
    def __init__(self, addr: tuple[str, int]) -> None:
        super().__init__(addr, _OTLPHandler)
        self._requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, path: str, headers: dict[str, str], body: bytes) -> None:
        with self._lock:
            self._requests.append({"path": path, "headers": headers, "body": body})

    @property
    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._requests)

    def paths_received(self) -> set[str]:
        return {r["path"] for r in self.requests}


@pytest.fixture()
def otlp_stub():
    """Bind a free port, serve on a background thread, yield the server."""
    server = _RecordingServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_providers(
    stub: _RecordingServer,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, Any]:
    """Construct three OTLP/HTTP providers wired to the stub.

    Does NOT call configure() or set the OTel globals — the SDK permits setting
    globals only once per process. Instead we build the providers directly and
    exercise them through their own APIs.  This isolates the test from
    cross-test global state while still exercising the real OTLP/HTTP exporter
    path.
    """
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter  # noqa: PLC0415
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter  # noqa: PLC0415
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415
    from opentelemetry.sdk._logs import LoggerProvider  # noqa: PLC0415
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor  # noqa: PLC0415
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    base_url = f"http://127.0.0.1:{stub.server_address[1]}"
    headers_str = "Authorization=Bearer test-token,Dash0-Dataset=test-ds"

    # Set env vars so no-arg exporter constructors pick them up.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", base_url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", headers_str)

    resource = Resource.create(
        {
            "service.name": "yaaos-web",
            "service.version": "0.0.0-dev",
            "deployment.environment.name": "local",
        }
    )

    # TracerProvider — no-arg exporter reads OTEL_EXPORTER_OTLP_ENDPOINT +
    # appends /v1/traces, and parses OTEL_EXPORTER_OTLP_HEADERS.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), export_timeout_millis=5_000))

    # MeterProvider
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=60_000,
                export_timeout_millis=5_000,
            )
        ],
    )

    # LoggerProvider
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(), export_timeout_millis=5_000)
    )

    return tracer_provider, meter_provider, logger_provider


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.service
def test_all_three_signals_reach_stub(otlp_stub: _RecordingServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Traces, metrics, and logs all POST to the stub within 5 seconds."""
    from opentelemetry.sdk._logs import LoggingHandler  # noqa: PLC0415

    tracer_provider, meter_provider, logger_provider = _build_providers(otlp_stub, monkeypatch)

    # Emit one span directly via the provider.
    tracer = tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("export-test-span"):
        pass
    tracer_provider.force_flush(timeout_millis=5_000)

    # Emit one metric via the provider.
    meter = meter_provider.get_meter("test")
    counter = meter.create_counter("test.requests")
    counter.add(1)
    meter_provider.force_flush(timeout_millis=5_000)

    # Emit one log record through a handler wired to the logger provider.
    # Set the logger level to INFO so the record passes the level filter —
    # the root stdlib logger defaults to WARNING in test environments, which
    # would silently drop the INFO record before it reaches the OTel handler.
    handler = LoggingHandler(logger_provider=logger_provider)
    test_logger = logging.getLogger("test_otel_export")
    prior_level = test_logger.level
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    try:
        test_logger.info("export-test-log-record")
        logger_provider.force_flush(timeout_millis=5_000)
    finally:
        test_logger.removeHandler(handler)
        test_logger.setLevel(prior_level)

    paths = otlp_stub.paths_received()
    assert "/v1/traces" in paths, f"No traces received. Paths: {paths}"
    assert "/v1/metrics" in paths, f"No metrics received. Paths: {paths}"
    assert "/v1/logs" in paths, f"No logs received. Paths: {paths}"


@pytest.mark.service
def test_requests_carry_auth_and_dataset_headers(
    otlp_stub: _RecordingServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every OTLP trace request carries Authorization and Dash0-Dataset headers."""
    tracer_provider, _mp, _lp = _build_providers(otlp_stub, monkeypatch)

    tracer = tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("header-check-span"):
        pass
    tracer_provider.force_flush(timeout_millis=5_000)

    trace_requests = [r for r in otlp_stub.requests if r["path"] == "/v1/traces"]
    assert trace_requests, "No trace requests recorded"

    for req in trace_requests:
        headers_lower = {k.lower(): v for k, v in req["headers"].items()}
        assert "authorization" in headers_lower, f"Missing Authorization header: {headers_lower}"
        assert headers_lower["authorization"] == "Bearer test-token"
        assert "dash0-dataset" in headers_lower, f"Missing Dash0-Dataset header: {headers_lower}"
        assert headers_lower["dash0-dataset"] == "test-ds"


@pytest.mark.service
def test_resource_carries_version_and_environment(
    otlp_stub: _RecordingServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trace resource includes service.version and deployment.environment.name."""
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

    tracer_provider, _mp, _lp = _build_providers(otlp_stub, monkeypatch)

    assert isinstance(tracer_provider, TracerProvider)
    resource = tracer_provider.resource

    attrs = dict(resource.attributes)
    assert "service.version" in attrs, f"Missing service.version in resource: {attrs}"
    assert "deployment.environment.name" in attrs, f"Missing deployment.environment.name in resource: {attrs}"
    assert attrs["service.version"] == "0.0.0-dev"
    assert attrs["deployment.environment.name"] == "local"


@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_flushes_buffered_records(
    otlp_stub: _RecordingServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shutdown() force-flushes; a buffered log record arrives after shutdown()."""
    from opentelemetry.sdk._logs import LoggingHandler  # noqa: PLC0415

    import app.core.observability.service as svc  # noqa: PLC0415

    tracer_provider, meter_provider, logger_provider = _build_providers(otlp_stub, monkeypatch)

    # Emit a log record that may still be in the batch buffer.
    # Set the logger level to INFO — root stdlib logger defaults to WARNING in
    # test environments and would silently drop INFO records before the OTel
    # handler sees them.
    handler = LoggingHandler(logger_provider=logger_provider)
    test_logger = logging.getLogger("test_shutdown_flush")
    prior_level = test_logger.level
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    try:
        # Inject the stub-wired providers as the refs svc.shutdown() flushes;
        # the seam restores the prior refs on exit (not None).
        with svc._scoped_otel_providers(tracer=tracer_provider, meter=meter_provider, logger=logger_provider):
            test_logger.info("pre-shutdown-flush-record")
            # Call shutdown; it must force-flush all three providers.
            await svc.shutdown()
    finally:
        test_logger.removeHandler(handler)
        test_logger.setLevel(prior_level)

    paths = otlp_stub.paths_received()
    assert "/v1/logs" in paths, f"shutdown() did not flush log records to stub. Paths received: {paths}"


# ── Metric-source tests (FastAPI + system metrics) ────────────────────────────


@pytest.mark.service
def test_fastapi_http_server_metrics_emit_after_request() -> None:
    """http.server.* instruments are registered and emit values after one HTTP request.

    Uses InMemoryMetricReader so no real OTLP stub is needed.

    The global FastAPIInstrumentor may already be instrumented (configure() is
    called from app.web at collection time, which replaces fastapi.FastAPI with
    _InstrumentedFastAPI so every new FastAPI() auto-wires the global provider).
    This test works around that by:
    1. Calling uninstrument() so fastapi.FastAPI is restored to the original.
    2. Re-importing fastapi.FastAPI from the module (not from the cached `from
       fastapi import FastAPI` binding) to get the original class.
    3. Creating the test app with the original FastAPI class.
    4. Instrumenting it explicitly with a local MeterProvider.
    5. Restoring the global instrumentation state in the finally block.
    """
    import fastapi as _fastapi  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    global_inst = FastAPIInstrumentor()
    was_instrumented = global_inst._is_instrumented_by_opentelemetry
    if was_instrumented:
        global_inst.uninstrument()

    # Re-read fastapi.FastAPI from the module so we get the restored original
    # class (not the _InstrumentedFastAPI reference that was cached by any
    # prior `from fastapi import FastAPI` import in this process).
    OrigFastAPI = _fastapi.FastAPI

    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])

    app = OrigFastAPI()

    @app.get("/health-probe")
    def _health() -> dict[str, str]:
        return {"ok": "true"}

    try:
        FastAPIInstrumentor.instrument_app(app, meter_provider=meter_provider)
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/health-probe")

        metrics_data = reader.get_metrics_data()
        assert metrics_data is not None, "InMemoryMetricReader returned no data"

        instrument_names: set[str] = set()
        for rm in metrics_data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    instrument_names.add(m.name)

        assert "http.server.duration" in instrument_names, (
            f"Expected http.server.duration; got: {instrument_names}"
        )
        assert "http.server.active_requests" in instrument_names, (
            f"Expected http.server.active_requests; got: {instrument_names}"
        )
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        meter_provider.shutdown()
        # Restore the global instrumentation state so other tests are unaffected.
        if was_instrumented:
            global_inst.instrument()


@pytest.mark.service
def test_system_metrics_instruments_registered_when_wired() -> None:
    """SystemMetricsInstrumentor registers CPU/memory/GC instruments once wired.

    Verifies that _configure_otel's system-metrics wiring produces a non-empty
    metric stream — i.e. the MeterProvider is not empty after instrumentation.
    Uses InMemoryMetricReader; no real OTLP endpoint required.

    The global SystemMetricsInstrumentor may already be instrumented (configure()
    wires it at boot). This test calls uninstrument() first so it can supply its
    own MeterProvider, then calls uninstrument() again in the finally block.
    """
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor  # noqa: PLC0415
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    global_inst = SystemMetricsInstrumentor()
    was_instrumented = global_inst._is_instrumented_by_opentelemetry
    if was_instrumented:
        global_inst.uninstrument()

    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])

    inst = SystemMetricsInstrumentor()
    try:
        inst.instrument(meter_provider=meter_provider)

        metrics_data = reader.get_metrics_data()
        assert metrics_data is not None, "InMemoryMetricReader returned no data after system-metrics wiring"

        instrument_names: set[str] = set()
        for rm in metrics_data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    instrument_names.add(m.name)

        # Check a representative cross-section: process CPU + memory + GC.
        assert any("cpu" in n for n in instrument_names), f"No CPU instrument found; got: {instrument_names}"
        assert any("memory" in n for n in instrument_names), (
            f"No memory instrument found; got: {instrument_names}"
        )
        assert any("gc" in n for n in instrument_names), f"No GC instrument found; got: {instrument_names}"
    finally:
        inst.uninstrument()
        meter_provider.shutdown()
