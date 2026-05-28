"""publish_after_commit ties an event to a caller's transaction outcome.

Commit → fire. Rollback → discard. The hook also has to work under the
SAVEPOINT-based rollback fixture used by service tests.
"""

import asyncio
from typing import Literal

import pytest

from app.core.events import (
    Event,
    EventFilter,
    publish_after_commit,
    subscribe,
)


class _SampleEvent(Event):
    kind: Literal["sample"] = "sample"
    source_module: Literal["test"] = "test"


@pytest.mark.asyncio
async def test_commit_flushes_pending_event(db_session) -> None:  # type: ignore[no-untyped-def]
    seen: list[Event] = []

    async def consume() -> None:
        async for ev in subscribe(EventFilter()):
            seen.append(ev)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    publish_after_commit(db_session, _SampleEvent())
    assert seen == []  # not yet — commit hasn't fired

    await db_session.commit()
    await asyncio.wait_for(consumer, timeout=1.0)

    assert len(seen) == 1
    assert seen[0].kind == "sample"


@pytest.mark.asyncio
async def test_rollback_discards_pending_event(db_session) -> None:  # type: ignore[no-untyped-def]
    seen: list[Event] = []

    async def consume() -> None:
        async for ev in subscribe(EventFilter()):
            seen.append(ev)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    publish_after_commit(db_session, _SampleEvent())
    await db_session.rollback()
    await asyncio.sleep(0.05)

    assert seen == []
    consumer.cancel()
