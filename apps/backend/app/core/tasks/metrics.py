"""Worker task metrics — taskiq middleware that records task execution signals.

Emits three OTel instruments per task execution, all dimensioned by task name
only (not org_id — cardinality would explode with a per-org label).

  task.started   — Counter: incremented once when a task body begins.
  task.succeeded — Counter: incremented when a task body returns cleanly.
  task.failed    — Counter: incremented when a task body raises an exception.
  task.duration  — Histogram (seconds): wall-clock time from pre_execute to
                   post_execute or on_error.

The module-level instruments are obtained via `metrics.get_meter(__name__)` at
import time. They resolve against whatever MeterProvider is global at that point
(typically a ProxyMeterProvider before `observability.configure()` runs), and
the OTel proxy SDK forwards calls to the real provider once one is set. In the
worker process, `observability.configure(role="worker")` sets a real
MeterProvider before any task body runs, so all recorded values reach the exporter.

`TaskMetricsMiddleware` is a taskiq `TaskiqMiddleware` wired into the broker by
`runtime.run()` alongside `OrgContextMiddleware`.

Test isolation: `TaskMetricsMiddleware` accepts optional instrument overrides in its
constructor so tests can inject instruments from a local `MeterProvider` backed by
an `InMemoryMetricReader`, without touching the global OTel provider state.
"""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram
from taskiq import TaskiqMessage, TaskiqMiddleware
from taskiq.result import TaskiqResult

_meter = metrics.get_meter(__name__)

_task_started: Counter = _meter.create_counter(
    name="task.started",
    description="Number of task executions that have begun.",
    unit="1",
)
_task_succeeded: Counter = _meter.create_counter(
    name="task.succeeded",
    description="Number of task executions that completed without error.",
    unit="1",
)
_task_failed: Counter = _meter.create_counter(
    name="task.failed",
    description="Number of task executions that raised an exception.",
    unit="1",
)
_task_duration: Histogram = _meter.create_histogram(
    name="task.duration",
    description="Wall-clock execution time for task bodies.",
    unit="s",
)


class TaskMetricsMiddleware(TaskiqMiddleware):
    """Records task.started / task.succeeded / task.failed + task.duration.

    By default uses the module-level instruments backed by the global OTel
    MeterProvider. Pass explicit instrument arguments to inject test-local
    instruments backed by an InMemoryMetricReader without mutating global
    OTel state.
    """

    def __init__(
        self,
        *,
        started: Counter | None = None,
        succeeded: Counter | None = None,
        failed: Counter | None = None,
        duration: Histogram | None = None,
    ) -> None:
        super().__init__()
        self._started = started if started is not None else _task_started
        self._succeeded = succeeded if succeeded is not None else _task_succeeded
        self._failed = failed if failed is not None else _task_failed
        self._duration = duration if duration is not None else _task_duration
        # Per-task-invocation start times keyed on taskiq task_id (a per-invocation UUID).
        # asyncio is cooperative; each task_id is unique, so no lock needed.
        self._start_times: dict[str, float] = {}

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        self._start_times[message.task_id] = time.monotonic()
        self._started.add(1, attributes={"task.name": message.task_name})
        return message

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        start = self._start_times.pop(message.task_id, None)
        attrs = {"task.name": message.task_name}
        if result.is_err:
            self._failed.add(1, attributes=attrs)
        else:
            self._succeeded.add(1, attributes=attrs)
        if start is not None:
            self._duration.record(time.monotonic() - start, attributes=attrs)

    async def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        start = self._start_times.pop(message.task_id, None)
        attrs = {"task.name": message.task_name}
        self._failed.add(1, attributes=attrs)
        if start is not None:
            self._duration.record(time.monotonic() - start, attributes=attrs)


# Module-level singleton wired into the broker at worker boot (see runtime.py).
task_metrics_middleware = TaskMetricsMiddleware()
