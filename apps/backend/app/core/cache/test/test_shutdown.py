"""Cache shutdown closes resources without opening clients or deleting shared state."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from typing import cast

import pytest
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.cache import (
    CacheError,
    delete_record,
    is_healthy,
    read_record,
    set_cache_for_tests,
    shutdown,
    update_record,
)
from app.core.cache.pubsub import _RedisProvider
from app.core.shutdown_registry import iter_web_shutdown_hooks, iter_worker_shutdown_hooks


class _Record(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_shutdown_clears_clients_and_is_idempotent(redis_or_skip) -> None:
    provider = _RedisProvider()
    with set_cache_for_tests(provider=provider):
        assert await is_healthy()
        assert provider._clients
        await shutdown()
        assert not provider._clients
        await shutdown()
        assert not provider._clients


@pytest.mark.asyncio
async def test_shutdown_without_state_does_not_initialize_clients() -> None:
    provider = _RedisProvider()
    with set_cache_for_tests(provider=provider):
        assert not provider._clients
        await shutdown()
        await shutdown()
        assert not provider._clients


def test_shutdown_registered_on_both_registries() -> None:
    assert shutdown in iter_worker_shutdown_hooks()
    assert shutdown in iter_web_shutdown_hooks()


@pytest.mark.asyncio
async def test_shutdown_does_not_clear_shared_data(bound_cache) -> None:
    key = f"cache-shutdown:{uuid.uuid4()}"
    try:
        await update_record(key, fields=_Record(value="survives"))
        await shutdown()
        assert await read_record(key) == {"value": "survives"}
    finally:
        await delete_record(key)
        await shutdown()


class _ClosingClient:
    def __init__(self, *, fails: bool) -> None:
        self.fails = fails
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
        if self.fails:
            raise RuntimeError("private-close-diagnostic")


@pytest.mark.asyncio
async def test_shutdown_drains_other_clients_and_sanitizes_partial_failure() -> None:
    provider = _RedisProvider()
    failed, healthy = _ClosingClient(fails=True), _ClosingClient(fails=False)
    other_loop = asyncio.new_event_loop()
    provider._clients[asyncio.get_running_loop()] = cast(Redis, failed)
    provider._clients[other_loop] = cast(Redis, healthy)
    try:
        with set_cache_for_tests(provider=provider):
            with pytest.raises(CacheError) as caught:
                await shutdown()
            assert "private-close-diagnostic" not in "".join(traceback.format_exception(caught.value))
            assert failed.closed == healthy.closed == 1
            assert not provider._clients
            await shutdown()
            assert failed.closed == healthy.closed == 1
    finally:
        other_loop.close()


@pytest.mark.asyncio
async def test_shutdown_tolerates_already_closed_owner_loop() -> None:
    provider = _RedisProvider()
    client = _ClosingClient(fails=True)
    owner = asyncio.new_event_loop()
    owner.close()
    provider._clients[owner] = cast(Redis, client)
    with set_cache_for_tests(provider=provider):
        await shutdown()
        assert client.closed == 1
        assert not provider._clients
        await shutdown()
        assert client.closed == 1


@pytest.mark.asyncio
async def test_worker_drain_does_not_crash(redis_or_skip) -> None:
    with set_cache_for_tests():
        assert await is_healthy()
        for hook in reversed(iter_worker_shutdown_hooks()):
            await hook()
