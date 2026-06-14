"""Service test: TaskSpanMiddleware records exception events + ERROR status on failing task spans.

A worker task body that raises must:
  - produce a span with status ERROR;
  - carry an exception event (exception.type, exception.message).

A task body that returns cleanly must:
  - produce a span with no exception events and non-ERROR status.

Uses a test-local TracerProvider + InMemorySpanExporter to isolate assertions
from global OTel state.  The InMemoryBroker + drain pattern mirrors
test_task_metrics_service.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from taskiq import InMemoryBroker

from app.core.tasks import drain_once, enqueue, task
from app.core.tasks.drain import _taskiq_dispatcher_for
from app.core.tasks.service import scoped_task_registration
from app.core.tasks.spans import TaskSpanMiddleware


def _exc_events(span):  # type: ignore[no-untyped-def]
    return [e for e in span.events if e.name == "exception"]


@pytest.mark.asyncio
@pytest.mark.service
async def test_failing_task_body_records_exception_event_and_error_status(db_session) -> None:  # type: ignore[no-untyped-def]
    """A task body that raises populates an exception event + ERROR status on the span.

    Flow:
    1. Build a test-local TracerProvider with InMemorySpanExporter.
    2. Inject a tracer from that provider into TaskSpanMiddleware.
    3. Register a task body that raises RuntimeError.
    4. Enqueue + drain into an InMemoryBroker wired with the middleware.
    5. Assert one finished span with ERROR status and an exception event.
    """
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test_task_span")

    middleware = TaskSpanMiddleware(tracer=tracer)

    task_name = f"span_fail_{uuid4().hex[:8]}"

    async def _boom_body() -> None:
        raise RuntimeError("intentional failure for span test")

    ref = task(task_name)(_boom_body)
    with scoped_task_registration(ref):
        broker = InMemoryBroker(await_inplace=True)
        broker.add_middlewares(middleware)
        broker.task(task_name=ref.name)(_boom_body)

        await broker.startup()
        try:
            await enqueue(ref, args={}, session=db_session)
            await db_session.commit()

            dispatcher = await _taskiq_dispatcher_for(broker)
            await drain_once(db_session, dispatcher=dispatcher)
            await db_session.commit()
        finally:
            await broker.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1, f"expected at least 1 span; got {[s.name for s in spans]}"

    # Find the task span (named after the task).
    task_spans = [s for s in spans if task_name in s.name]
    assert len(task_spans) >= 1, f"expected a span containing {task_name!r}; got {[s.name for s in spans]}"
    span = task_spans[0]

    assert span.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status on failing task; got {span.status.status_code}"
    )
    exc_events = _exc_events(span)
    assert len(exc_events) >= 1, f"expected at least one exception event; got {span.events}"
    attrs = exc_events[0].attributes or {}
    assert "RuntimeError" in attrs.get("exception.type", ""), (
        f"exception.type should contain 'RuntimeError'; got {attrs}"
    )
    assert "intentional failure for span test" in attrs.get("exception.message", ""), (
        f"exception.message should contain the message; got {attrs}"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_successful_task_body_has_no_exception_event(db_session) -> None:  # type: ignore[no-untyped-def]
    """A task body that returns cleanly produces a span without exception events."""
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test_task_span_ok")

    middleware = TaskSpanMiddleware(tracer=tracer)

    task_name = f"span_ok_{uuid4().hex[:8]}"

    async def _noop_body() -> None:
        pass

    ref = task(task_name)(_noop_body)
    with scoped_task_registration(ref):
        broker = InMemoryBroker(await_inplace=True)
        broker.add_middlewares(middleware)
        broker.task(task_name=ref.name)(_noop_body)

        await broker.startup()
        try:
            await enqueue(ref, args={}, session=db_session)
            await db_session.commit()

            dispatcher = await _taskiq_dispatcher_for(broker)
            await drain_once(db_session, dispatcher=dispatcher)
            await db_session.commit()
        finally:
            await broker.shutdown()

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1, f"expected at least 1 span; got {[s.name for s in spans]}"

    task_spans = [s for s in spans if task_name in s.name]
    assert len(task_spans) >= 1, f"expected a span containing {task_name!r}; got {[s.name for s in spans]}"
    span = task_spans[0]

    exc_events = _exc_events(span)
    assert len(exc_events) == 0, f"unexpected exception events on successful task: {span.events}"
    assert span.status.status_code != StatusCode.ERROR, (
        f"expected non-ERROR status on successful task; got {span.status.status_code}"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_span_created_in_task_body_nests_under_task_span(db_session) -> None:  # type: ignore[no-untyped-def]
    """A span opened inside the task body is a child of the task span.

    Regression guard: `pre_execute` must `context.attach` the task span, not
    just `start_span` it. Without the attach, spans created during the body
    (auto-instrumentation, manual spans) parent off the dequeue-time context
    and the task span is effectively orphaned.
    """
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test_task_span_nesting")

    middleware = TaskSpanMiddleware(tracer=tracer)

    task_name = f"span_nest_{uuid4().hex[:8]}"

    async def _body_with_child() -> None:
        # A span opened inside the body should pick up the task span as parent
        # via the current context that pre_execute attached.
        with tracer.start_as_current_span("child-work"):
            pass

    ref = task(task_name)(_body_with_child)
    with scoped_task_registration(ref):
        broker = InMemoryBroker(await_inplace=True)
        broker.add_middlewares(middleware)
        broker.task(task_name=ref.name)(_body_with_child)

        await broker.startup()
        try:
            await enqueue(ref, args={}, session=db_session)
            await db_session.commit()

            dispatcher = await _taskiq_dispatcher_for(broker)
            await drain_once(db_session, dispatcher=dispatcher)
            await db_session.commit()
        finally:
            await broker.shutdown()

    spans = exporter.get_finished_spans()
    task_spans = [s for s in spans if task_name in s.name]
    child_spans = [s for s in spans if s.name == "child-work"]
    assert len(task_spans) == 1, f"expected one task span; got {[s.name for s in spans]}"
    assert len(child_spans) == 1, f"expected one child span; got {[s.name for s in spans]}"

    task_span = task_spans[0]
    child_span = child_spans[0]
    assert child_span.parent is not None, "child span should have a parent (the task span)"
    assert child_span.parent.span_id == task_span.context.span_id, "child span must nest under the task span"
    assert child_span.context.trace_id == task_span.context.trace_id, (
        "child span must share the task span's trace"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_task_span_uses_metadata_traceparent_as_parent(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: when `TaskMetadata.traceparent` is present in the
    message labels, `TaskSpanMiddleware.pre_execute` opens the `task:<name>` span
    as a child of the encoded remote span — placing it in the producer's trace.

    Flow:
    1. Open a producer span in a test-local TracerProvider.
    2. Capture its traceparent string.
    3. Build a `TaskiqMessage` whose `metadata` label carries that traceparent.
    4. Call `TaskSpanMiddleware.pre_execute` directly (no broker needed).
    5. Assert the resulting `task:<name>` span shares the producer's trace_id.
    """
    from taskiq import TaskiqMessage  # noqa: PLC0415

    from app.core.observability import current_traceparent  # noqa: PLC0415
    from app.core.tasks import TaskMetadata  # noqa: PLC0415

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test_task_span_traceparent_parent")

    # Open a producer span to capture its traceparent.
    with tracer.start_as_current_span("producer-span") as producer_span:
        producer_trace_id = producer_span.get_span_context().trace_id
        producer_tp = current_traceparent()

    assert producer_tp is not None, "current_traceparent() must return a value inside a span"

    # Build metadata with the producer's traceparent.
    meta = TaskMetadata(org_id=None, traceparent=producer_tp)
    meta_json = meta.model_dump_json()

    middleware = TaskSpanMiddleware(tracer=tracer)

    task_name = f"tp_parent_{uuid4().hex[:8]}"
    task_id = uuid4().hex

    # Simulate a taskiq message carrying the metadata label.
    msg = TaskiqMessage(
        task_id=task_id,
        task_name=task_name,
        labels={"metadata": meta_json},
        args=[],
        kwargs={},
    )

    # Run pre_execute to open the task span.
    await middleware.pre_execute(msg)

    # Simulate the task completing (we don't care about outcome here).
    from taskiq.result import TaskiqResult  # noqa: PLC0415

    result: TaskiqResult[None] = TaskiqResult(is_err=False, log=None, return_value=None, execution_time=0.0)
    await middleware.post_execute(msg, result)

    spans = exporter.get_finished_spans()
    task_spans = [s for s in spans if task_name in s.name]
    assert len(task_spans) == 1, f"expected one task span; got {[s.name for s in spans]}"

    task_span = task_spans[0]
    assert task_span.context.trace_id == producer_trace_id, (
        f"task span trace_id {task_span.context.trace_id:032x} != "
        f"producer trace_id {producer_trace_id:032x}; "
        "task span must be in the producer's trace when traceparent is supplied"
    )
    assert task_span.parent is not None, "task span must have a parent (the producer span)"
