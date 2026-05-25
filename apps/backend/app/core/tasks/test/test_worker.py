"""Worker scaffolding: escaped child-task exceptions are logged.

`worker.run()` waits on drain + consume + stop tasks via `FIRST_COMPLETED`.
`drain_loop` and `Receiver.listen` both catch their own exceptions, so
normally nothing escapes — but if one does, the exception object would be
discarded when the task is GC'd unless we explicitly retrieve it. These
tests pin the retrieve-and-log behavior.

Uses `structlog.testing.capture_logs()` rather than a fixture that swaps
processors — `observability.configure()` sets `cache_logger_on_first_use`,
so by the time these tests run other suites may have already cached the
worker's bound logger. `capture_logs` is the cache-safe API.
"""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from app.core.tasks.worker import _log_done_task_exceptions


@pytest.mark.asyncio
async def test_logs_exception_from_done_task() -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    task = asyncio.create_task(boom(), name="boomer")
    with pytest.raises(RuntimeError):
        await task

    with capture_logs() as logs:
        _log_done_task_exceptions({task})

    crashed = [e for e in logs if e.get("event") == "tasks.worker.child_crashed"]
    assert len(crashed) == 1
    assert crashed[0]["task"] == "boomer"
    assert crashed[0]["exc_info"][0] is RuntimeError


@pytest.mark.asyncio
async def test_no_log_for_normal_completion() -> None:
    async def fine() -> None:
        return None

    task = asyncio.create_task(fine(), name="finisher")
    await task

    with capture_logs() as logs:
        _log_done_task_exceptions({task})

    assert all(e.get("event") != "tasks.worker.child_crashed" for e in logs)


@pytest.mark.asyncio
async def test_no_log_for_cancelled_task() -> None:
    async def slow() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(slow(), name="sleeper")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with capture_logs() as logs:
        _log_done_task_exceptions({task})

    assert all(e.get("event") != "tasks.worker.child_crashed" for e in logs)


@pytest.mark.asyncio
async def test_logs_each_failing_task() -> None:
    async def boom_a() -> None:
        raise ValueError("a")

    async def boom_b() -> None:
        raise KeyError("b")

    async def ok() -> None:
        return None

    a = asyncio.create_task(boom_a(), name="task_a")
    b = asyncio.create_task(boom_b(), name="task_b")
    c = asyncio.create_task(ok(), name="task_c")
    await asyncio.gather(a, b, c, return_exceptions=True)

    with capture_logs() as logs:
        _log_done_task_exceptions({a, b, c})

    crashed = [e for e in logs if e.get("event") == "tasks.worker.child_crashed"]
    names = sorted(e["task"] for e in crashed)
    assert names == ["task_a", "task_b"]
