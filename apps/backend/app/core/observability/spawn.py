"""`spawn()` + `active_task_count()` — fire-and-forget background-coroutine helper.

Wraps `asyncio.create_task` with an OTel span + try/except that:
  - records `span.record_exception(exc)` + `set_status(ERROR)` on exception;
  - logs `spawn.crashed` at ERROR (existing behavior, preserved);
  - does NOT re-raise (contract unchanged — the coro marks its domain row
    failed before raising; `spawn()` is the last-resort safety net).

Lives under `core/observability` because the job is exception visibility for
background tasks — both logs and traces become unusable without it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode, Tracer

log = structlog.get_logger("observability.spawn")

_tracer = trace.get_tracer(__name__)

# Module-level set keeps spawned tasks alive (asyncio's standard pitfall —
# without a strong reference, the GC may collect them mid-flight).
_tasks: set[asyncio.Task[Any]] = set()


def spawn(
    name: str,
    coro: Coroutine[Any, Any, None],
    *,
    tracer: Tracer | None = None,
) -> asyncio.Task[Any]:
    """Fire-and-forget background work.

    Opens a span named `spawn:{name}` around the coroutine body. On exception:
      1. `span.record_exception(exc)` — attaches the exception as a span event.
      2. `span.set_status(ERROR)` — marks the trace as failed.
      3. logs `spawn.crashed` with a stack trace (existing safety net).

    The coroutine is expected to mark its own domain row failed before raising.
    `spawn()` catches as a last resort; it never re-raises.

    `tracer` is an optional injection point for tests — pass a tracer backed by
    an `InMemorySpanExporter` to assert on span state without touching the global
    OTel provider. Production callers omit it; the module-level proxy tracer is used.
    """
    _t = tracer if tracer is not None else _tracer

    async def _wrapper() -> None:
        with _t.start_as_current_span(f"spawn:{name}") as span:
            try:
                await coro
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                logging.getLogger("yaaos").exception("spawn.crashed", extra={"spawn_name": name})

    task = asyncio.create_task(_wrapper(), name=f"spawn:{name}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.debug("spawn.started", spawn_name=name)
    return task


def active_task_count() -> int:
    """Test helper — number of pending spawned tasks."""
    return sum(1 for t in _tasks if not t.done())


__all__ = ["active_task_count", "spawn"]
