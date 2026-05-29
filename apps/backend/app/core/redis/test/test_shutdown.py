"""core/redis.shutdown — closes the client cache and drops the pub/sub
singleton; registered on both web and worker shutdown registries."""

from __future__ import annotations

import pytest

import app.core.redis.pubsub as _pubsub
import app.core.redis.service as _svc
from app.core.redis import reset_pubsub, shutdown
from app.core.redis.pubsub import get_pubsub
from app.core.redis.service import _get_client, _reset_clients_for_tests
from app.core.shutdown_registry import iter_web_shutdown_hooks, iter_worker_shutdown_hooks


@pytest.fixture(autouse=True)
async def _isolate():
    _reset_clients_for_tests()
    reset_pubsub()
    yield
    await _svc.shutdown()
    reset_pubsub()


@pytest.mark.asyncio
async def test_shutdown_clears_clients_and_singleton(redis_or_skip) -> None:
    """After shutdown() the client cache is empty and the bus singleton is gone."""
    _get_client()  # warm the cache
    get_pubsub()  # materialize the singleton
    assert _svc._clients, "expected cache populated before shutdown"
    assert _pubsub._singleton is not None

    await shutdown()
    assert not _svc._clients
    assert _pubsub._singleton is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(redis_or_skip) -> None:
    """Calling shutdown() twice does not raise."""
    _get_client()
    await shutdown()
    await shutdown()  # must not raise


@pytest.mark.asyncio
async def test_shutdown_idempotent_without_state() -> None:
    """shutdown() on an empty cache + no singleton is a no-op."""
    assert not _svc._clients
    await shutdown()  # must not raise


def test_shutdown_registered_on_both_registries() -> None:
    """core/redis registers shutdown() on the web and worker shutdown registries."""
    assert shutdown in iter_worker_shutdown_hooks()
    assert shutdown in iter_web_shutdown_hooks()


@pytest.mark.asyncio
async def test_worker_drain_drops_singleton(redis_or_skip) -> None:
    """Draining the worker registry invokes shutdown, dropping the bus singleton."""
    get_pubsub()  # materialize the singleton
    assert _pubsub._singleton is not None

    for hook in reversed(iter_worker_shutdown_hooks()):
        await hook()

    assert _pubsub._singleton is None
