"""core.agent_gateway.shutdown — drops subscriber registry singleton."""

from __future__ import annotations

import pytest

from app.core.agent_gateway.subscribers import _reset_for_tests, get_registry, shutdown


@pytest.fixture(autouse=True)
def _isolate():
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.mark.asyncio
async def test_shutdown_drops_singleton() -> None:
    """After shutdown() the singleton is None."""
    import app.core.agent_gateway.subscribers as subs  # noqa: PLC0415

    get_registry()  # materialize singleton
    assert subs._singleton is not None

    await shutdown()
    assert subs._singleton is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()
