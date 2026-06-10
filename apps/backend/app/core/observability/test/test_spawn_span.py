"""spawn() records exceptions on the wrapping OTel span.

A spawned coroutine that raises must:
  - produce a span with status ERROR;
  - carry an exception event with the exception type and message.

The test-local TracerProvider + InMemorySpanExporter pattern isolates the
assertions from whatever global OTel provider state other test modules leave.
spawn() accepts an optional `tracer=` kwarg so tests can inject a local
tracer; production callers use the default (global tracer for the module).
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.core.observability import spawn


@pytest.fixture()
def span_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    return provider, exporter


@pytest.mark.asyncio
async def test_spawn_records_exception_and_error_status_on_span(
    span_exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    """A crashing spawned coroutine records an exception event + ERROR status on its span."""
    provider, exporter = span_exporter
    tracer = provider.get_tracer("test_spawn_span")

    async def _boom() -> None:
        raise RuntimeError("spawn crashed for test")

    task = spawn("test_crash_span", _boom(), tracer=tracer)
    await task  # wrapper must not re-raise

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected 1 span; got {[s.name for s in spans]}"
    span = spans[0]

    # Status must be ERROR.
    assert span.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status; got {span.status.status_code}"
    )

    # At least one exception event must be recorded.
    exc_events = [e for e in span.events if e.name == "exception"]
    assert len(exc_events) >= 1, f"expected at least one exception event; got {span.events}"
    event = exc_events[0]
    assert "RuntimeError" in (event.attributes or {}).get("exception.type", ""), (
        f"exception.type should contain 'RuntimeError'; got {event.attributes}"
    )
    assert "spawn crashed for test" in (event.attributes or {}).get("exception.message", ""), (
        f"exception.message should contain the message; got {event.attributes}"
    )


@pytest.mark.asyncio
async def test_spawn_does_not_produce_span_on_success(
    span_exporter: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    """A successful spawned coroutine produces a span with OK/UNSET status and no exception events."""
    provider, exporter = span_exporter
    tracer = provider.get_tracer("test_spawn_span_ok")

    done = asyncio.Event()

    async def _ok() -> None:
        done.set()

    task = spawn("test_ok_span", _ok(), tracer=tracer)
    await task
    assert done.is_set()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected 1 span; got {[s.name for s in spans]}"
    span = spans[0]

    # No exception events on a clean run.
    exc_events = [e for e in span.events if e.name == "exception"]
    assert len(exc_events) == 0, f"unexpected exception events on success: {span.events}"
    # Status should not be ERROR.
    assert span.status.status_code != StatusCode.ERROR, (
        f"expected non-ERROR status on success; got {span.status.status_code}"
    )
