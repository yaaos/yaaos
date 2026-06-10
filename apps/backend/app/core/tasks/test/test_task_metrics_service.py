"""Service test: TaskMetricsMiddleware increments task counters and records duration.

Verifies the worker task metrics signals dimensioned by task name:
  - task.started is incremented once per execution.
  - task.succeeded is incremented when the body returns cleanly.
  - task.failed is incremented when the body raises an exception.
  - task.duration records a non-negative value on each execution.

Uses a test-local MeterProvider backed by InMemoryMetricReader to observe
instrument values without touching the global OTel provider state.  The
InMemoryBroker + drain pattern mirrors test_middleware_service.py.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from taskiq import InMemoryBroker

from app.core.tasks import drain_once, enqueue, task
from app.core.tasks.drain import _taskiq_dispatcher_for
from app.core.tasks.metrics import TaskMetricsMiddleware
from app.core.tasks.service import scoped_task_registration


def _read_metric_values(reader: InMemoryMetricReader, name: str) -> dict[frozenset[tuple[str, str]], Any]:
    """Return a mapping of frozenset(attributes) → data-point value for `name`."""
    data = reader.get_metrics_data()
    if data is None:
        return {}
    result: dict[frozenset[tuple[str, str]], Any] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != name:
                    continue
                for dp_set in metric.data.data_points:
                    key = frozenset((k, str(v)) for k, v in dp_set.attributes.items())
                    # Sum-type (Counter) has .value; Histogram has .sum.
                    val = getattr(dp_set, "value", None) or getattr(dp_set, "sum", None)
                    if val is not None:
                        result[key] = val
    return result


def _attrs(task_name: str) -> frozenset[tuple[str, str]]:
    return frozenset({("task.name", task_name)})


@pytest.mark.asyncio
@pytest.mark.service
async def test_success_increments_started_and_succeeded_and_records_duration(db_session) -> None:  # type: ignore[no-untyped-def]
    """A task body that returns cleanly increments started + succeeded + duration.

    Flow:
    1. Build a test-local MeterProvider with InMemoryMetricReader.
    2. Create fresh instruments and inject them into TaskMetricsMiddleware.
    3. Register a no-op task body.
    4. Enqueue + drain into an InMemoryBroker wired with the middleware.
    5. Assert counter values and a non-zero duration.
    """
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    test_meter = meter_provider.get_meter("test_task_metrics")

    started = test_meter.create_counter("task.started")
    succeeded = test_meter.create_counter("task.succeeded")
    failed = test_meter.create_counter("task.failed")
    duration = test_meter.create_histogram("task.duration")

    middleware = TaskMetricsMiddleware(
        started=started,
        succeeded=succeeded,
        failed=failed,
        duration=duration,
    )

    task_name = f"metrics_success_{uuid4().hex[:8]}"

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

    attrs = _attrs(task_name)
    started_vals = _read_metric_values(reader, "task.started")
    succeeded_vals = _read_metric_values(reader, "task.succeeded")
    failed_vals = _read_metric_values(reader, "task.failed")
    duration_vals = _read_metric_values(reader, "task.duration")

    assert started_vals.get(attrs, 0) == 1, f"task.started should be 1 for {task_name!r}; got {started_vals}"
    assert succeeded_vals.get(attrs, 0) == 1, (
        f"task.succeeded should be 1 for {task_name!r}; got {succeeded_vals}"
    )
    assert failed_vals.get(attrs, 0) == 0, f"task.failed should be 0 for {task_name!r}; got {failed_vals}"
    assert (duration_vals.get(attrs, -1) or 0) >= 0, (
        f"task.duration should be >= 0 for {task_name!r}; got {duration_vals}"
    )

    meter_provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.service
async def test_failing_body_increments_failed_and_records_duration(db_session) -> None:  # type: ignore[no-untyped-def]
    """A task body that raises an exception increments task.failed and records duration.

    InMemoryBroker with await_inplace=True surfaces exceptions as is_err results
    (not re-raised); the middleware's on_error or post_execute(is_err=True) path runs.
    """
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    test_meter = meter_provider.get_meter("test_task_metrics_fail")

    started = test_meter.create_counter("task.started")
    succeeded = test_meter.create_counter("task.succeeded")
    failed = test_meter.create_counter("task.failed")
    duration = test_meter.create_histogram("task.duration")

    middleware = TaskMetricsMiddleware(
        started=started,
        succeeded=succeeded,
        failed=failed,
        duration=duration,
    )

    task_name = f"metrics_fail_{uuid4().hex[:8]}"

    async def _boom_body() -> None:
        raise RuntimeError("intentional failure for metrics test")

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

    attrs = _attrs(task_name)
    started_vals = _read_metric_values(reader, "task.started")
    failed_vals = _read_metric_values(reader, "task.failed")
    succeeded_vals = _read_metric_values(reader, "task.succeeded")
    duration_vals = _read_metric_values(reader, "task.duration")

    assert started_vals.get(attrs, 0) == 1, f"task.started should be 1 for {task_name!r}; got {started_vals}"
    assert failed_vals.get(attrs, 0) >= 1, f"task.failed should be >= 1 for {task_name!r}; got {failed_vals}"
    assert succeeded_vals.get(attrs, 0) == 0, (
        f"task.succeeded should be 0 for {task_name!r}; got {succeeded_vals}"
    )
    assert (duration_vals.get(attrs, -1) or 0) >= 0, (
        f"task.duration should be >= 0 for {task_name!r}; got {duration_vals}"
    )

    meter_provider.shutdown()
