"""Worker process entrypoint.

Boots one event loop with three asyncio tasks raced via
`asyncio.wait(..., FIRST_COMPLETED)`:
  - the outbox drain loop — pushes pending outbox rows into the broker
  - `Receiver.listen` — consumes tasks from the taskiq broker
  - a `stop.wait()` task tied to SIGTERM/SIGINT

Whichever finishes first triggers shutdown of the others. Normally that's
the stop-signal task; the other two finishing means something escaped a
handler ([`_log_done_task_exceptions`] logs it). Single-process POC; the
drain/consume pair split into separate compose services later if scale
demands it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Iterable

import structlog
from taskiq.receiver import Receiver

from app.core import database, observability
from app.core.shutdown_registry import iter_worker_shutdown_hooks
from app.core.tasks.broker import get_broker
from app.core.tasks.drain import drain_loop
from app.core.tasks.middleware import org_context_middleware

log = structlog.get_logger("core.tasks.worker")


def _log_done_task_exceptions(done: Iterable[asyncio.Task[object]]) -> None:
    """Surface exceptions that escaped a child coroutine's own handlers.

    `drain_loop` and `Receiver.listen` catch their own errors, so this only
    fires on the escape path (e.g. a bug in the except branch itself).
    Calling `.exception()` also suppresses asyncio's GC-time "Task exception
    was never retrieved" stderr warning — this is the single source of
    truth for these failures, not a duplicate of any inner log line.
    """
    for t in done:
        if t.cancelled():
            continue
        exc = t.exception()
        if exc is not None:
            log.error(
                "tasks.worker.child_crashed",
                task=t.get_name(),
                exc_info=(type(exc), exc, exc.__traceback__),
            )


async def run() -> None:
    """Worker process body. Migrate the schema, import modules that carry
    `@task` decorators (registers them with the broker as a side-effect),
    then run drain + consumer side by side. Cancels both gracefully on
    SIGTERM/SIGINT.
    """
    observability.configure(role="worker")
    await database.migrate()

    broker = get_broker()
    # Import each module whose `@task` decorators register task bodies
    # with the broker. The decorator runs at import time and calls
    # `broker.task(...)` — no separate bind step needed.
    import app.core.workflow  # noqa: F401, PLC0415

    broker.add_middlewares(org_context_middleware)
    log.info("tasks.worker.booting", broker=type(broker).__name__)

    await broker.startup()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    drain_task = asyncio.create_task(drain_loop(broker), name="drain_loop")
    # `broker.listen()` is an async-generator yielding raw broker messages.
    # `Receiver` wraps it: consumes the generator, parses each message,
    # looks the registered @task body up by name, and dispatches.
    # `Receiver.listen(finish_event)` runs until the event is set; we
    # tie it to the same `stop` event the SIGTERM/SIGINT handler triggers.
    receiver = Receiver(broker, run_startup=False)
    consume_task = asyncio.create_task(receiver.listen(stop), name="broker_listen")
    stop_task = asyncio.create_task(stop.wait(), name="stop_signal")

    log.info("tasks.worker.running")
    done, pending = await asyncio.wait(
        {drain_task, consume_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    log.info(
        "tasks.worker.shutting_down",
        finished=[t.get_name() for t in done],
    )
    _log_done_task_exceptions(done)
    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    # Shut down all registered worker-process modules in reverse-registration order.
    for hook in reversed(iter_worker_shutdown_hooks()):
        with contextlib.suppress(Exception):
            await hook()
    log.info("tasks.worker.stopped")
