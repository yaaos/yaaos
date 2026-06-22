"""core/redis.shutdown — closes the client cache; registered on both web and
worker shutdown registries."""

from __future__ import annotations

import pytest

import app.core.redis.service as _svc
from app.core.redis import shutdown
from app.core.redis.pubsub import _get, set_pubsub_for_tests
from app.core.redis.service import _get_client, _reset_clients_for_tests
from app.core.shutdown_registry import iter_web_shutdown_hooks, iter_worker_shutdown_hooks


@pytest.fixture(autouse=True)
async def _isolate():
    """Use a fresh pubsub instance and reset client cache for each test."""
    _reset_clients_for_tests()
    with set_pubsub_for_tests():
        yield
        await _svc.shutdown()


@pytest.mark.asyncio
async def test_shutdown_clears_clients(redis_or_skip) -> None:
    """After shutdown() the client cache is empty."""
    _get_client()  # warm the cache
    _get()  # verify pubsub is reachable before shutdown
    assert _svc._clients, "expected cache populated before shutdown"

    await shutdown()
    assert not _svc._clients


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(redis_or_skip) -> None:
    """Calling shutdown() twice does not raise."""
    _get_client()
    await shutdown()
    await shutdown()  # must not raise


@pytest.mark.asyncio
async def test_shutdown_idempotent_without_state() -> None:
    """shutdown() on an empty cache is a no-op."""
    assert not _svc._clients
    await shutdown()  # must not raise


def test_shutdown_registered_on_both_registries() -> None:
    """core/redis registers shutdown() on the web and worker shutdown registries."""
    assert shutdown in iter_worker_shutdown_hooks()
    assert shutdown in iter_web_shutdown_hooks()


@pytest.mark.asyncio
async def test_worker_drain_does_not_crash(redis_or_skip) -> None:
    """Draining the worker registry invokes shutdown without crashing."""
    _get()  # prove the pubsub is accessible
    for hook in reversed(iter_worker_shutdown_hooks()):
        await hook()
    # After all hooks: pubsub still accessible via ContextVar (shutdown does
    # not clear the ContextVar — it calls aclose() on the active instance only).
    # The _isolate fixture's CM restores the prior binding after yield.
