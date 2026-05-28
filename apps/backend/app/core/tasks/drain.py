"""Outbox drain — the Postgres → Redis pump.

`drain_once` pulls undispatched outbox rows, hands each to its dispatcher,
and stamps `dispatched_at` on success. Failures bump `attempt` + record
`last_error` and leave the row pending. Idempotent: a crash between
dispatch and the stamp update redispatches on the next poll, so task
bodies must tolerate duplicates.

`drain_loop` is the long-running coroutine the worker runs; it dispatches
`kind='taskiq_enqueue'` rows to the taskiq broker (pushes to Redis). Other
kinds are logged + left for a future dispatcher to handle.

The SELECT uses `FOR UPDATE SKIP LOCKED` so multiple worker processes
don't double-dispatch the same row — the safety holds even with a single
worker and is cheap to keep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import AsyncBroker

from app.core.database import session
from app.core.tasks.models import OutboxEntryRow
from app.core.tasks.types import TaskMetadata

log = structlog.get_logger("core.tasks.drain")

Dispatcher = Callable[[str, dict[str, Any]], Awaitable[None]]


async def write(
    db_session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any],
) -> UUID:
    """Insert an outbox row in `db_session`. Returns the new row id.

    Internal API — callers go through `enqueue()` in service.py rather
    than touching outbox rows directly. Kept module-public so service.py
    can call it without circular-import gymnastics.
    """
    if not kind:
        raise ValueError("outbox write requires a non-empty kind")
    row = OutboxEntryRow(id=uuid4(), kind=kind, payload=payload)
    db_session.add(row)
    await db_session.flush()
    return row.id


async def drain_once(
    db_session: AsyncSession,
    *,
    dispatcher: Dispatcher,
    batch_size: int = 100,
) -> int:
    """Pull up to `batch_size` undispatched rows, hand each to `dispatcher`,
    stamp `dispatched_at` on success. Failure on a row leaves it pending
    with `attempt` and `last_error` updated. Uses `FOR UPDATE SKIP LOCKED`
    so concurrent drain processes never grab the same row.
    """
    rows = (
        (
            await db_session.execute(
                select(OutboxEntryRow)
                .where(OutboxEntryRow.dispatched_at.is_(None))
                .order_by(OutboxEntryRow.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    delivered = 0
    for row in rows:
        try:
            await dispatcher(row.kind, row.payload)
        except Exception as exc:
            log.warning(
                "tasks.drain.dispatch_failed",
                outbox_id=str(row.id),
                kind=row.kind,
                error=str(exc),
            )
            await db_session.execute(
                update(OutboxEntryRow)
                .where(OutboxEntryRow.id == row.id)
                .values(attempt=row.attempt + 1, last_error=str(exc)[:1000])
            )
            continue
        await db_session.execute(
            update(OutboxEntryRow).where(OutboxEntryRow.id == row.id).values(dispatched_at=datetime.now())
        )
        delivered += 1
    return delivered


async def _taskiq_dispatcher_for(broker: AsyncBroker) -> Dispatcher:
    """Return a dispatcher that routes `kind='taskiq_enqueue'` payloads
    to the taskiq broker. Other kinds raise — they need their own
    dispatcher registered upstream (the drain handles only what it
    knows).
    """

    async def dispatch(kind: str, payload: dict[str, Any]) -> None:
        if kind != "taskiq_enqueue":
            raise ValueError(f"no dispatcher registered for kind={kind!r}")
        task_name = payload.get("task_name")
        args = payload.get("args") or {}
        metadata = payload.get("metadata")
        if not isinstance(task_name, str) or not task_name:
            raise ValueError("taskiq_enqueue payload missing task_name")
        if not isinstance(args, dict):
            raise ValueError("taskiq_enqueue args must be a dict")
        task = broker.find_task(task_name)
        if task is None:
            raise ValueError(f"taskiq task not registered: {task_name}")
        kicker = task.kicker()
        if metadata is not None:
            # Encode as a JSON string so taskiq's label serializer
            # (which `str()`s non-primitive values) doesn't mangle it.
            # The middleware parses with `TaskMetadata.model_validate_json`.
            meta_json = TaskMetadata.model_validate(metadata).model_dump_json()
            kicker = kicker.with_labels(metadata=meta_json)
        await kicker.kiq(**args)

    return dispatch


async def drain_loop(broker: AsyncBroker, *, poll_idle_seconds: float = 0.1) -> None:
    """Long-running coroutine: poll undispatched outbox rows and ship
    them to the broker. Sleeps `poll_idle_seconds` between empty polls;
    immediately re-polls when a batch had work."""
    dispatcher = await _taskiq_dispatcher_for(broker)
    while True:
        try:
            async with session() as s:
                n = await drain_once(s, dispatcher=dispatcher)
                await s.commit()
        except Exception:
            log.exception("tasks.drain.loop_crashed")
            await asyncio.sleep(1.0)
            continue
        await asyncio.sleep(0 if n > 0 else poll_idle_seconds)
