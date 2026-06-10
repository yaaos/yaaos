"""Worker process entrypoint.

Boots one event loop with five asyncio tasks raced via
`asyncio.wait(..., FIRST_COMPLETED)`:
  - the outbox drain loop — pushes pending outbox rows into the broker
  - `Receiver.listen` — consumes tasks from the taskiq broker
  - the recurring-task scheduler tick loop
  - the liveness ticker — updates the shared heartbeat every
    `TICKER_INTERVAL_SECONDS` so the health server can report freshness
  - a `stop.wait()` task tied to SIGTERM/SIGINT

Additionally, a background `uvicorn.Server` task runs a minimal
single-route Starlette app on `0.0.0.0:<yaaos_worker_health_port>` (default
`8081`).  Fly's machine checker hits that port directly (bypassing
Cloudflare) to restart a wedged worker.

Whichever finishes first triggers shutdown of the others. Normally that's
the stop-signal task; the other tasks finishing means something escaped a
handler ([`_log_done_task_exceptions`] logs it). Single-process setup; the
drain/consume pair can split into separate compose services later if scale
demands it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Iterable

import structlog
import uvicorn
from taskiq.receiver import Receiver

from app.core import database, observability
from app.core.config import get_settings
from app.core.shutdown_registry import iter_worker_shutdown_hooks
from app.core.tasks.broker import get_broker
from app.core.tasks.drain import drain_loop
from app.core.tasks.metrics import task_metrics_middleware
from app.core.tasks.middleware import org_context_middleware
from app.core.tasks.scheduler import scheduler_loop
from app.core.tasks.spans import task_span_middleware
from app.core.tasks.worker_health import TICKER_INTERVAL_SECONDS, WorkerHeartbeat, build_worker_health_app

log = structlog.get_logger("core.tasks.worker")


async def _liveness_ticker(heartbeat: WorkerHeartbeat, stop: asyncio.Event) -> None:
    """Advance the worker heartbeat every `TICKER_INTERVAL_SECONDS`.

    Runs as a background asyncio task inside `run()`.  On each wake the
    heartbeat records `time.monotonic()` via `tick()`; if the task is ever
    cancelled or `stop` is set the loop exits cleanly.  The health handler
    checks that the last tick is within `_STALE_THRESHOLD_SECONDS`; a
    wedged consume loop means this task also stops, causing the health
    check to return 503 within two missed ticks.
    """
    while not stop.is_set():
        heartbeat.tick()
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICKER_INTERVAL_SECONDS)
        except TimeoutError:
            pass


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
    then run drain + consumer + scheduler + liveness ticker side by side.
    Also runs a background minimal health server on the worker health port.
    Cancels all tasks gracefully on SIGTERM/SIGINT.
    """
    observability.configure(role="worker")
    await database.migrate()

    broker = get_broker()
    # Task-defining modules are loaded by the composition root (`app/worker.py`)
    # before `run()` is called — `@task` decorators are already registered here.
    broker.add_middlewares(org_context_middleware, task_metrics_middleware, task_span_middleware)
    log.info("tasks.worker.booting", broker=type(broker).__name__)

    await broker.startup()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    # Shared heartbeat — the liveness ticker advances it; the health handler reads it.
    heartbeat = WorkerHeartbeat()
    health_port = get_settings().yaaos_worker_health_port
    health_app = build_worker_health_app(heartbeat=heartbeat)
    health_config = uvicorn.Config(
        health_app,
        host="0.0.0.0",  # S104 not enabled — intentionally world-accessible for Fly's machine checker
        port=health_port,
        log_level="warning",
        access_log=False,
    )
    health_server = uvicorn.Server(health_config)
    health_task = asyncio.create_task(health_server.serve(), name="health_server")

    # Liveness ticker — advances the heartbeat every TICKER_INTERVAL_SECONDS.
    ticker_task = asyncio.create_task(_liveness_ticker(heartbeat, stop), name="liveness_ticker")

    drain_task = asyncio.create_task(drain_loop(broker), name="drain_loop")
    # `broker.listen()` is an async-generator yielding raw broker messages.
    # `Receiver` wraps it: consumes the generator, parses each message,
    # looks the registered @task body up by name, and dispatches.
    # `Receiver.listen(finish_event)` runs until the event is set; we
    # tie it to the same `stop` event the SIGTERM/SIGINT handler triggers.
    receiver = Receiver(broker, run_startup=False)
    consume_task = asyncio.create_task(receiver.listen(stop), name="broker_listen")
    # Recurring-task scheduler tick loop — cluster-safe via per-tick
    # claim on `scheduled_runs(schedule_id, fire_time)`. Every worker
    # runs this; only the slot-winner enqueues.
    scheduler_task = asyncio.create_task(scheduler_loop(), name="scheduler_loop")
    stop_task = asyncio.create_task(stop.wait(), name="stop_signal")

    log.info("tasks.worker.running", health_port=health_port)
    done, pending = await asyncio.wait(
        {drain_task, consume_task, scheduler_task, stop_task, ticker_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    log.info(
        "tasks.worker.shutting_down",
        finished=[t.get_name() for t in done],
    )
    _log_done_task_exceptions(done)

    # Signal the health server to stop before cancelling other pending tasks.
    health_server.should_exit = True

    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t

    # Wait for the health server to finish (it shuts down on should_exit=True).
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await health_task

    # Shut down all registered worker-process modules in reverse-registration order.
    for hook in reversed(iter_worker_shutdown_hooks()):
        with contextlib.suppress(Exception):
            await hook()
    log.info("tasks.worker.stopped")
