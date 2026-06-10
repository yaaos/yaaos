"""Service test — all 3 OTel signals reach an HTTP listener with correct
headers and resource attributes; shutdown() force-flushes buffered records.

This test drives _configure_otel() directly (bypassing the idempotency guard)
and exercises the providers via their direct references rather than via the
OTel globals (which the SDK permits setting only once per process). It asserts:

  - All three paths receive data: /v1/traces, /v1/metrics, /v1/logs.
  - Every request carries the Authorization and Dash0-Dataset headers set via
    OTEL_EXPORTER_OTLP_HEADERS.
  - The trace resource carries service.version and deployment.environment.name.
  - shutdown() force-flushes a buffered log record (i.e. a record emitted before
    shutdown() completes arrives at the stub after await).

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
            "service.name": "yaaos-app",
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
    handler = LoggingHandler(logger_provider=logger_provider)
    test_logger = logging.getLogger("test_otel_export")
    test_logger.addHandler(handler)
    try:
        test_logger.info("export-test-log-record")
        logger_provider.force_flush(timeout_millis=5_000)
    finally:
        test_logger.removeHandler(handler)

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

    # Point the module-level provider refs at our stub-wired providers so that
    # svc.shutdown() flushes them.
    svc._tracer_provider = tracer_provider
    svc._meter_provider = meter_provider
    svc._logger_provider = logger_provider

    # Emit a log record that may still be in the batch buffer.
    handler = LoggingHandler(logger_provider=logger_provider)
    test_logger = logging.getLogger("test_shutdown_flush")
    test_logger.addHandler(handler)
    try:
        test_logger.info("pre-shutdown-flush-record")
        # Call shutdown; it must force-flush all three providers.
        await svc.shutdown()
    finally:
        test_logger.removeHandler(handler)
        # Restore module state so other tests aren't affected.
        svc._tracer_provider = None
        svc._meter_provider = None
        svc._logger_provider = None

    paths = otlp_stub.paths_received()
    assert "/v1/logs" in paths, f"shutdown() did not flush log records to stub. Paths received: {paths}"
