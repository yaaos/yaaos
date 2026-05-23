"""Worker process entrypoint.

Boots one event loop with:
  - the taskiq broker (Redis-backed) — consumes tasks from the queue
  - the outbox drain loop — pushes pending outbox rows into the broker

Both run as asyncio tasks via `asyncio.gather`. Single-process POC; the
two responsibilities split into separate compose services later if/when
scale demands it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from app.core import database
from app.core.tasks.broker import get_broker
from app.core.tasks.drain import drain_loop

log = structlog.get_logger("core.tasks.worker")


async def run() -> None:
    """Worker process body. Migrate the schema, register all `@task`
    bodies with the broker, then run drain + consumer side by side.
    Cancels both gracefully on SIGTERM/SIGINT.
    """
    await database.migrate()

    broker = get_broker()
    # Import the modules that register @task bodies so the in-process
    # registry is populated before the broker starts dispatching. Future
    # task-defining modules add themselves here.
    # (Phase 1: no in-tree @task bodies yet; this is the seam.)
    log.info("tasks.worker.booting", broker=type(broker).__name__)

    await broker.startup()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    drain_task = asyncio.create_task(drain_loop(broker), name="drain_loop")
    # taskiq's listen() consumes from the broker and dispatches to registered
    # task bodies. It blocks until the broker shuts down.
    consume_task = asyncio.create_task(broker.listen(), name="broker_listen")
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
    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    with contextlib.suppress(Exception):
        await broker.shutdown()
    await database.dispose()
    log.info("tasks.worker.stopped")
