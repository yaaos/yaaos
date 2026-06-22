"""OTel in-process span capture for tests.

Provides `span_capture()` — a context manager that installs an
`InMemorySpanExporter` on a fresh `TracerProvider` (or the existing one)
and yields it. Spans emitted inside the `with` block are available via
`exporter.get_finished_spans()`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@contextmanager
def span_capture() -> Iterator[InMemorySpanExporter]:
    """Capture OTel spans emitted inside the `with` block.

    Installs an `InMemorySpanExporter` with a `SimpleSpanProcessor` on the
    current global `TracerProvider`. If no SDK provider has been set (e.g.
    the test session hasn't called `observability.configure()`), a minimal
    `TracerProvider` is created and set as the global provider.

    Yields the `InMemorySpanExporter`. Call `.get_finished_spans()` after
    the `with` block to inspect captured spans.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
