"""OTel SDK wiring + structlog trace-context injection."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.observability.service import _inject_trace_context


@pytest.fixture
def in_memory_spans():
    """Provide a local TracerProvider with an in-memory exporter.

    Creates a fresh TracerProvider so the fixture is independent of whether
    configure() has been called (the global provider may still be a proxy if
    these tests run without a prior configure() call). Tests call
    provider.get_tracer("test") to emit spans.
    """
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    yield provider, exporter
    processor.shutdown()


def test_inject_trace_context_no_active_span() -> None:
    """Outside a span, the processor should not add trace_id/span_id."""
    out = _inject_trace_context(None, "info", {"event": "boot"})
    assert "trace_id" not in out
    assert "span_id" not in out


def test_inject_trace_context_inside_span() -> None:
    # Use a local TracerProvider so the test is independent of whether
    # configure() has been called (global provider may still be a proxy).
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("unit-test-span"):
        out = _inject_trace_context(None, "info", {"event": "doing-work"})
    assert "trace_id" in out
    assert "span_id" in out
    assert len(out["trace_id"]) == 32  # 128-bit hex
    assert len(out["span_id"]) == 16  # 64-bit hex


def test_in_memory_exporter_captures_spans(in_memory_spans: tuple) -> None:
    """Verify the in-memory fixture actually works — emit a span, see it."""
    provider, exporter = in_memory_spans
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("captured-span"):
        pass
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "captured-span" in names
