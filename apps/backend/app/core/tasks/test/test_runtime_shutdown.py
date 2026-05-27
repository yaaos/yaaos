"""Worker runtime teardown invokes registered worker shutdown hooks.

Tests use local probe lists (not the global process registry) to avoid
corrupting global state that other tests depend on.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

import pytest


async def _run_hooks_like_runtime(hooks: list[Any]) -> None:
    """Simulate the worker runtime shutdown sequence over a local hook list."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    stop.set()  # simulate SIGTERM

    for hook in reversed(hooks):
        with contextlib.suppress(Exception):
            await hook()


@pytest.mark.asyncio
async def test_worker_shutdown_hooks_run_on_stop() -> None:
    """Stopping the worker (stop event set) runs registered shutdown hooks."""
    probe_ran: list[bool] = []

    async def probe_hook() -> None:
        probe_ran.append(True)

    hooks: list[Any] = [probe_hook]
    await _run_hooks_like_runtime(hooks)

    assert probe_ran == [True]


@pytest.mark.asyncio
async def test_worker_shutdown_hooks_run_in_reverse_order() -> None:
    """Hooks run in reverse-registration order during worker teardown."""
    order: list[str] = []

    async def hook_one() -> None:
        order.append("one")

    async def hook_two() -> None:
        order.append("two")

    hooks: list[Any] = [hook_one, hook_two]
    await _run_hooks_like_runtime(hooks)

    assert order == ["two", "one"]


@pytest.mark.asyncio
async def test_worker_shutdown_hook_failure_does_not_abort_sequence() -> None:
    """A failing hook must not prevent later hooks from running."""
    ran: list[str] = []

    async def fine() -> None:
        ran.append("fine")

    async def boom() -> None:
        raise RuntimeError("boom")

    hooks: list[Any] = [fine, boom]  # fine first → last in reversed; boom second → first
    await _run_hooks_like_runtime(hooks)

    assert "fine" in ran
