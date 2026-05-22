"""OTel SDK wiring + structlog trace-context injection."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.observability.service import _inject_trace_context


@pytest.fixture
def in_memory_spans():
    """Attach an in-memory exporter to the global TracerProvider, yield it,
    then detach. Tests pull spans out via `exporter.get_finished_spans()`."""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "TracerProvider not initialized — call observability.configure() first"
    )
    provider.add_span_processor(processor)
    yield exporter
    processor.shutdown()


def test_inject_trace_context_no_active_span() -> None:
    """Outside a span, the processor should not add trace_id/span_id."""
    out = _inject_trace_context(None, "info", {"event": "boot"})
    assert "trace_id" not in out
    assert "span_id" not in out


def test_inject_trace_context_inside_span() -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("unit-test-span"):
        out = _inject_trace_context(None, "info", {"event": "doing-work"})
    assert "trace_id" in out
    assert "span_id" in out
    assert len(out["trace_id"]) == 32  # 128-bit hex
    assert len(out["span_id"]) == 16  # 64-bit hex


def test_in_memory_exporter_captures_spans(in_memory_spans: InMemorySpanExporter) -> None:
    """Verify the in-memory fixture actually works — emit a span, see it."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("captured-span"):
        pass
    spans = in_memory_spans.get_finished_spans()
    names = {s.name for s in spans}
    assert "captured-span" in names
