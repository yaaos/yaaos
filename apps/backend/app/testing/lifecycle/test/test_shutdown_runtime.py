"""testing/lifecycle.shutdown_runtime — deduplicated hook dispatch at session end."""

from __future__ import annotations

import pytest

from app.core.shutdown_registry import _web_shutdown_hooks, _worker_shutdown_hooks
from app.core.tasks import register_worker_shutdown_hook
from app.core.webserver import register_web_shutdown_hook
from app.testing.lifecycle import shutdown_runtime


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot both registries before each test and restore after."""
    web_original = list(_web_shutdown_hooks)
    worker_original = list(_worker_shutdown_hooks)
    yield
    _web_shutdown_hooks.clear()
    _web_shutdown_hooks.extend(web_original)
    _worker_shutdown_hooks.clear()
    _worker_shutdown_hooks.extend(worker_original)


@pytest.mark.asyncio
async def test_both_registries_are_called() -> None:
    """Hooks registered with both web and worker registries are each called once."""
    web_calls: list[str] = []
    worker_calls: list[str] = []

    async def _web_hook() -> None:
        web_calls.append("web")

    async def _worker_hook() -> None:
        worker_calls.append("worker")

    register_web_shutdown_hook(_web_hook)
    register_worker_shutdown_hook(_worker_hook)

    await shutdown_runtime()

    assert web_calls == ["web"]
    assert worker_calls == ["worker"]


@pytest.mark.asyncio
async def test_duplicate_hook_runs_only_once() -> None:
    """Same hook object registered with both web and worker runs exactly once."""
    calls: list[str] = []

    async def _shared_hook() -> None:
        calls.append("run")

    register_web_shutdown_hook(_shared_hook)
    register_worker_shutdown_hook(_shared_hook)

    await shutdown_runtime()

    assert calls == ["run"]


@pytest.mark.asyncio
async def test_hook_exception_does_not_abort_remaining() -> None:
    """An exception from one hook does not prevent subsequent hooks from running."""
    after_calls: list[str] = []

    async def _bad_hook() -> None:
        raise RuntimeError("probe failure")

    async def _good_hook() -> None:
        after_calls.append("ran")

    # bad registered first (executes last due to reversed order)
    register_web_shutdown_hook(_bad_hook)
    register_web_shutdown_hook(_good_hook)

    await shutdown_runtime()  # must not raise

    assert after_calls == ["ran"]
