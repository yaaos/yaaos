"""Outbox/drain coverage — folded in from the old core/outbox tests.

Drives `write()` + `drain_once()` directly with a stub dispatcher (no
taskiq broker required). The broker-wired dispatcher is covered as part
of the worker's e2e test path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.tasks import drain_once
from app.core.tasks.drain import write
from app.core.tasks.models import OutboxEntryRow


@pytest.mark.asyncio
async def test_write_inserts_undispatched_row(db_session) -> None:
    row_id = await write(db_session, kind="taskiq_enqueue", payload={"hello": "world"})
    await db_session.commit()

    row = (await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.id == row_id))).scalar_one()
    assert row.kind == "taskiq_enqueue"
    assert row.payload == {"hello": "world"}
    assert row.dispatched_at is None
    assert row.attempt == 0


@pytest.mark.asyncio
async def test_drain_dispatches_and_stamps(db_session) -> None:
    await write(db_session, kind="taskiq_enqueue", payload={"x": 1})
    await write(db_session, kind="taskiq_enqueue", payload={"x": 2})
    await db_session.commit()

    delivered: list[dict] = []

    async def dispatcher(kind: str, payload: dict) -> None:
        delivered.append(payload)

    n = await drain_once(db_session, dispatcher=dispatcher)
    await db_session.commit()
    assert n == 2
    assert {p["x"] for p in delivered} == {1, 2}

    remaining = (
        (await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.dispatched_at.is_(None))))
        .scalars()
        .all()
    )
    assert remaining == []


@pytest.mark.asyncio
async def test_drain_failure_leaves_row_undispatched(db_session) -> None:
    row_id = await write(db_session, kind="taskiq_enqueue", payload={"x": 1})
    await db_session.commit()

    async def failing(kind: str, payload: dict) -> None:
        raise RuntimeError("boom")

    n = await drain_once(db_session, dispatcher=failing)
    await db_session.commit()
    assert n == 0

    row = (await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.id == row_id))).scalar_one()
    assert row.dispatched_at is None
    assert row.attempt == 1
    assert row.last_error and "boom" in row.last_error
