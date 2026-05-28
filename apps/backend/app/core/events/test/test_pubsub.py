import asyncio
from typing import Literal
from uuid import uuid4

import pytest

from app.core.events import Event, EventFilter, publish, subscribe


class _SampleEvent(Event):
    kind: Literal["sample"] = "sample"
    source_module: Literal["test"] = "test"


@pytest.mark.asyncio
async def test_subscriber_receives_matching_event() -> None:
    seen: list[Event] = []

    async def consume() -> None:
        async for ev in subscribe(EventFilter()):
            seen.append(ev)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)  # let subscribe register
    await publish(_SampleEvent())
    await consumer
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_filter_by_kind() -> None:
    seen_a: list[Event] = []
    seen_b: list[Event] = []

    class A(Event):
        kind: Literal["a"] = "a"
        source_module: Literal["test"] = "test"

    class B(Event):
        kind: Literal["b"] = "b"
        source_module: Literal["test"] = "test"

    async def consume_a() -> None:
        async for ev in subscribe(EventFilter(kinds=["a"])):
            seen_a.append(ev)
            return

    async def consume_b() -> None:
        async for ev in subscribe(EventFilter(kinds=["b"])):
            seen_b.append(ev)
            return

    ca = asyncio.create_task(consume_a())
    cb = asyncio.create_task(consume_b())
    await asyncio.sleep(0.01)
    await publish(A())
    await publish(B())
    await asyncio.wait_for(asyncio.gather(ca, cb), timeout=1.0)
    assert len(seen_a) == 1 and seen_a[0].kind == "a"
    assert len(seen_b) == 1 and seen_b[0].kind == "b"


@pytest.mark.asyncio
async def test_filter_by_ticket() -> None:
    seen: list[Event] = []
    tid = uuid4()
    other = uuid4()

    async def consume() -> None:
        async for ev in subscribe(EventFilter(ticket_id=tid)):
            seen.append(ev)
            return

    c = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await publish(_SampleEvent(ticket_id=other))  # filtered out
    await publish(_SampleEvent(ticket_id=tid))
    await c
    assert seen[0].ticket_id == tid
