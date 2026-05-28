"""core.sse_pubsub.shutdown — drops singleton (public API smoke test)."""

from __future__ import annotations

import pytest

import app.core.sse_pubsub.service as _svc
from app.core.sse_pubsub.service import get_pubsub, shutdown


@pytest.fixture(autouse=True)
def _isolate():
    _svc._singleton = None
    yield
    _svc._singleton = None


@pytest.mark.asyncio
async def test_shutdown_drops_singleton() -> None:
    """After shutdown() the singleton is None."""
    get_pubsub()  # materialize singleton
    assert _svc._singleton is not None

    await shutdown()
    assert _svc._singleton is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()
