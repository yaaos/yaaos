"""Web shutdown registry — registration order and failure isolation."""

from __future__ import annotations

import pytest

from app.core.shutdown_registry import (
    _web_shutdown_hooks,
    iter_web_shutdown_hooks,
    register_web_shutdown_hook,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot the registry before each test and restore after."""
    original = list(_web_shutdown_hooks)
    yield
    _web_shutdown_hooks.clear()
    _web_shutdown_hooks.extend(original)


@pytest.mark.asyncio
async def test_hooks_returned_in_registration_order() -> None:
    calls: list[str] = []

    async def hook_a() -> None:
        calls.append("a")

    async def hook_b() -> None:
        calls.append("b")

    register_web_shutdown_hook(hook_a)
    register_web_shutdown_hook(hook_b)

    hooks = iter_web_shutdown_hooks()
    for hook in hooks:
        await hook()

    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_failing_hook_does_not_prevent_subsequent_hooks() -> None:
    """A failing hook must not prevent later hooks from running.

    The caller (app_factory) wraps each hook in try/except, but the registry
    itself returns all hooks — this test asserts registration order is complete
    even when hooks raise.
    """
    calls: list[str] = []

    async def boom() -> None:
        raise RuntimeError("explosion")

    async def after_boom() -> None:
        calls.append("after")

    register_web_shutdown_hook(boom)
    register_web_shutdown_hook(after_boom)

    hooks = iter_web_shutdown_hooks()
    for hook in hooks:
        try:
            await hook()
        except Exception:
            pass

    assert calls == ["after"]


def test_iter_returns_snapshot_not_live_list() -> None:
    """Modifying the returned list must not affect the registry."""

    async def hook_x() -> None:
        pass

    register_web_shutdown_hook(hook_x)
    snapshot = iter_web_shutdown_hooks()
    snapshot.clear()

    # Registry must still have hook_x
    assert any(h is hook_x for h in iter_web_shutdown_hooks())
