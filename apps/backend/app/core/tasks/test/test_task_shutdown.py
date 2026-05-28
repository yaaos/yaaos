"""core.tasks.shutdown — graceful broker connection teardown."""

from __future__ import annotations

import pytest

from app.core.tasks.service import shutdown


@pytest.mark.asyncio
async def test_shutdown_does_not_drop_broker_singleton() -> None:
    """shutdown() keeps the broker singleton in place so task registrations survive."""
    import app.core.tasks.broker as broker_mod  # noqa: PLC0415

    original = broker_mod.get_broker()

    await shutdown()

    # Singleton must still exist (same object).
    assert broker_mod._broker is original


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()
