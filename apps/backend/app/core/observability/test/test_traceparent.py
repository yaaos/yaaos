"""traceparent helpers + span continuity across the boundary."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.observability import (
    current_traceparent,
    restore_traceparent_context,
    with_remote_parent_span,
)


@pytest.fixture
def in_memory_spans():
    """Provide a local TracerProvider with an in-memory exporter.

    Creates a fresh TracerProvider so the fixture is independent of whether
    configure() has been called. The yielded exporter captures spans from
    tracers created via the yielded provider.
    """
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    yield provider, exporter
    processor.shutdown()


def test_current_traceparent_returns_none_without_span() -> None:
    """Outside any span the helper returns None — boot-time logs and
    request handlers without observability don't synthesize a context."""
    assert current_traceparent() is None


def test_current_traceparent_inside_span_is_well_formed() -> None:
    # Use a local TracerProvider so the test is independent of whether
    # configure() has been called (global provider may still be a proxy).
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("unit-traceparent"):
        tp = current_traceparent()
    assert tp is not None
    parts = tp.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"  # W3C version
    assert len(parts[1]) == 32  # trace_id hex
    assert len(parts[2]) == 16  # span_id hex
    assert parts[3] in {"00", "01"}  # flags


def test_restore_traceparent_context_returns_valid_context() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("upstream"):
        upstream = current_traceparent()
    assert upstream is not None
    ctx = restore_traceparent_context(upstream)
    assert ctx is not None
    restored_span = trace.get_current_span(ctx)
    assert restored_span.get_span_context().is_valid


def test_restore_traceparent_context_rejects_empty_or_malformed() -> None:
    assert restore_traceparent_context("") is None
    assert restore_traceparent_context(None) is None
    # Malformed inputs (wrong length, bad hex) should also yield None.
    assert restore_traceparent_context("not-a-traceparent") is None


def test_span_emitted_under_remote_parent_inherits_trace_id(
    in_memory_spans: tuple,
) -> None:
    """Open a span; capture its traceparent; in a *fresh* context, open
    a span under the captured parent. Both spans must share the same
    `trace_id`."""
    provider, exporter = in_memory_spans
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("origin") as origin:
        origin_traceparent = current_traceparent()
        origin_trace_id = origin.get_span_context().trace_id
    assert origin_traceparent is not None

    # `origin` has ended. Now emit a "downstream" span as a child of the
    # captured traceparent — what a task body would do after pulling the
    # traceparent off its arguments.
    with with_remote_parent_span(tracer, "downstream", origin_traceparent) as downstream:
        downstream_trace_id = downstream.get_span_context().trace_id

    assert downstream_trace_id == origin_trace_id

    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    assert "origin" in by_name
    assert "downstream" in by_name
    # Both belong to the same trace.
    assert by_name["origin"].context.trace_id == by_name["downstream"].context.trace_id


def test_with_remote_parent_span_without_traceparent_starts_fresh_trace(
    in_memory_spans: tuple,
) -> None:
    """When no traceparent is supplied (None / empty), the helper still
    emits a span — just under a fresh trace."""
    provider, exporter = in_memory_spans
    tracer = provider.get_tracer("test")
    with with_remote_parent_span(tracer, "fresh", None) as span:
        assert span.get_span_context().is_valid
    spans = exporter.get_finished_spans()
    assert any(s.name == "fresh" for s in spans)
