"""Outbox primitive + drain loop.

`write(session, kind, payload)` inserts an `outbox_entries` row in the
caller's session. Caller commits; drain delivers.

`drain_once(session, *, dispatcher)` reads undispatched rows and hands them
to a caller-supplied dispatcher coroutine `(kind, payload) -> None`. Rows are
marked dispatched only on success; failures bump `attempt` and leave the row
for the next poll. Idempotent: the worker task body must tolerate duplicate
delivery (a crash between dispatch and update redispatches the row).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.outbox.models import OutboxEntryRow

log = structlog.get_logger("core.outbox")

Dispatcher = Callable[[str, dict[str, Any]], Awaitable[None]]


async def write(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any],
) -> UUID:
    """Insert an outbox row in `session`. Returns the new row id. The caller
    commits the session; the drain delivers after commit."""
    if not kind:
        raise ValueError("outbox.write requires a non-empty kind")
    row = OutboxEntryRow(id=uuid4(), kind=kind, payload=payload)
    session.add(row)
    await session.flush()
    return row.id


async def drain_once(
    session: AsyncSession,
    *,
    dispatcher: Dispatcher,
    batch_size: int = 100,
) -> int:
    """Pull up to `batch_size` undispatched rows, hand each to `dispatcher`,
    and stamp `dispatched_at` on success. Returns the number successfully
    dispatched. Failure on a row leaves it for the next call (with
    `attempt` and `last_error` updated)."""
    rows = (
        (
            await session.execute(
                select(OutboxEntryRow)
                .where(OutboxEntryRow.dispatched_at.is_(None))
                .order_by(OutboxEntryRow.created_at)
                .limit(batch_size)
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
            log.warning("outbox.dispatch_failed", outbox_id=str(row.id), kind=row.kind, error=str(exc))
            await session.execute(
                update(OutboxEntryRow)
                .where(OutboxEntryRow.id == row.id)
                .values(attempt=row.attempt + 1, last_error=str(exc)[:1000])
            )
            continue
        await session.execute(
            update(OutboxEntryRow).where(OutboxEntryRow.id == row.id).values(dispatched_at=datetime.now())
        )
        delivered += 1
    return delivered
