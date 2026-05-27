"""core.sse_pubsub.shutdown — drops singleton (public API smoke test)."""

from __future__ import annotations

import pytest

from app.core.sse_pubsub.service import _reset_for_tests, get_pubsub, shutdown


@pytest.fixture(autouse=True)
def _isolate():
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.mark.asyncio
async def test_shutdown_drops_singleton() -> None:
    """After shutdown() the singleton is None."""
    import app.core.sse_pubsub.service as svc  # noqa: PLC0415

    get_pubsub()  # materialize singleton
    assert svc._singleton is not None

    await shutdown()
    assert svc._singleton is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()
