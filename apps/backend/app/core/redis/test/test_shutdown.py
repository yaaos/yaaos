"""core.redis.shutdown — clears the client cache (public API smoke test)."""

from __future__ import annotations

import pytest

from app.core.redis.service import _clients, _reset_for_tests, get_client, shutdown


@pytest.fixture(autouse=True)
async def _isolate():
    _reset_for_tests()
    yield
    from app.core.redis.service import aclose  # noqa: PLC0415

    await aclose()


@pytest.mark.asyncio
async def test_shutdown_clears_clients(redis_or_skip) -> None:
    """After shutdown() the _clients cache is empty."""
    get_client()  # warm the cache
    assert _clients, "expected cache to be populated before shutdown"
    await shutdown()
    assert not _clients, "expected cache to be empty after shutdown"


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(redis_or_skip) -> None:
    """Calling shutdown() twice does not raise."""
    get_client()
    await shutdown()
    await shutdown()  # must not raise


@pytest.mark.asyncio
async def test_shutdown_idempotent_without_clients() -> None:
    """shutdown() on an empty cache is a no-op."""
    assert not _clients
    await shutdown()  # must not raise
