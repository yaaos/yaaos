"""In-process OTel span capture for tests.

Provides `span_capture()` — a context manager that installs an
`InMemorySpanExporter` on a fresh `TracerProvider` (or the existing one)
and yields it. Spans emitted inside the `with` block are available via
`exporter.get_finished_spans()`.

Usage (inside a `@pytest.mark.service` test)::

    from app.testing.observability import span_capture

    async def test_something() -> None:
        with span_capture() as exporter:
            # drive the code under test
            ...
        spans = exporter.get_finished_spans()
        err_spans = [s for s in spans if s.name == "workflow.command.MyKind"]
        assert err_spans[0].status.status_code == StatusCode.ERROR

Design notes:

- NOT in `__all__` of any production module — this file lives under
  `app/testing/` and is imported only by test code.
- Uses `SimpleSpanProcessor` (synchronous export on span end) so
  `get_finished_spans()` is immediately consistent without a flush.
- Installs the processor on whichever `TracerProvider` is currently
  global; if none has been set yet it creates a minimal one and sets it
  as the global provider. This mirrors the pattern in the existing
  `test_trace_linkage.py::in_memory_spans` fixture.
- On context-manager exit the processor is shut down (draining any
  remaining spans) but the provider is NOT replaced — the provider is
  process-global and the processor list grows monotonically. The
  exporter's span list is cleared between tests when a fresh
  `span_capture()` is opened.
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
