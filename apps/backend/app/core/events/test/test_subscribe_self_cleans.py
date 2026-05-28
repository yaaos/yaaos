"""subscribe() self-cleanup — async generator unregisters on consumer exit."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Literal

import pytest

from app.core.events import EventFilter, publish, subscribe, subscriber_count
from app.core.events.service import Event


class _PingEvent(Event):
    kind: Literal["ping"] = "ping"
    source_module: Literal["test"] = "test"


@pytest.mark.asyncio
@pytest.mark.service
async def test_async_for_return_cleans_up() -> None:
    """Consumer that returns from inside async for leaves no subscriber behind.

    `return` inside `async for` causes the generator's `finally` to fire via
    async-generator finalizer scheduling — the subscriber is removed by the
    time the next event-loop tick runs.
    """
    before = subscriber_count()

    async def _consume() -> None:
        async for _ev in subscribe(EventFilter(kinds=["ping"])):
            return  # exits the coroutine; async-gen finalizer schedules aclose()

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.01)
    assert subscriber_count() == before + 1

    await publish(_PingEvent())
    await asyncio.wait_for(consumer, timeout=1.0)
    # Yield to let the async-gen finalizer fire aclose() on the generator.
    await asyncio.sleep(0)

    assert subscriber_count() == before


@pytest.mark.asyncio
@pytest.mark.service
async def test_aclosing_cleans_up_on_early_exit() -> None:
    """async with aclosing(...) fires the generator's finally on exit."""
    before = subscriber_count()

    async def _consume_one() -> None:
        async with aclosing(subscribe(EventFilter(kinds=["ping"]))) as gen:
            await asyncio.sleep(0.01)  # let subscription register
            # publish so anext() returns
            _task = asyncio.ensure_future(publish(_PingEvent()))  # noqa: RUF006
            await gen.__anext__()
            # exit the `async with` block — aclosing calls aclose()

    consumer = asyncio.create_task(_consume_one())
    await asyncio.wait_for(consumer, timeout=2.0)

    assert subscriber_count() == before
