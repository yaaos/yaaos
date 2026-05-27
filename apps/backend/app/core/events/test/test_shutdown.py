"""core.events.shutdown — clears subscriber registry (public API smoke test)."""

from __future__ import annotations

import asyncio

import pytest

from app.core.events.service import _reset_for_tests, _subscribers, shutdown


@pytest.fixture(autouse=True)
def _isolate():
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.mark.asyncio
async def test_shutdown_clears_subscribers() -> None:
    """After shutdown() the subscriber dict is empty."""
    from app.core.events.service import EventFilter  # noqa: PLC0415

    # Simulate a registered subscriber by adding directly.
    _subscribers["fake-sub"] = (EventFilter(), asyncio.Queue())
    assert _subscribers

    await shutdown()
    assert not _subscribers


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()
