import asyncio

import pytest

from app.core.observability import active_task_count, spawn


@pytest.mark.asyncio
async def test_spawn_runs_coroutine() -> None:
    done = asyncio.Event()

    async def work() -> None:
        done.set()

    task = spawn("test", work())
    await task
    assert done.is_set()


@pytest.mark.asyncio
async def test_spawn_catches_exceptions() -> None:
    async def crashing() -> None:
        raise RuntimeError("boom")

    task = spawn("test_crash", crashing())
    await task  # should not raise; wrapper swallowed it


@pytest.mark.asyncio
async def test_active_task_count() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def waiter() -> None:
        started.set()
        await release.wait()

    task = spawn("waiter", waiter())
    await started.wait()
    assert active_task_count() >= 1
    release.set()
    await task
