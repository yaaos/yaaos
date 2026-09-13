"""JSON event contract, readiness handshakes, and deterministic iterator cleanup."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import aclosing

import pytest

from app.core.cache import publish, set_cache_for_tests, subscribe, subscriber_count
from app.core.cache.pubsub import _RedisProvider


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_returns_zero(bound_cache) -> None:
    assert await publish(f"cache-events:{uuid.uuid4()}", {"event": "x"}) == 0


@pytest.mark.asyncio
async def test_publish_fans_out_to_every_ready_subscriber(bound_cache) -> None:
    channel = f"cache-fanout:{uuid.uuid4()}"
    ready_a, ready_b = asyncio.Event(), asyncio.Event()
    received_a, received_b = [], []

    async def consume(target, ready) -> None:
        async with aclosing(subscribe(channel, on_subscribed=ready)) as events:
            target.append(await anext(events))
            target.append(await anext(events))

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        tasks.create_task(consume(received_a, ready_a))
        tasks.create_task(consume(received_b, ready_b))
        await asyncio.wait_for(asyncio.gather(ready_a.wait(), ready_b.wait()), 2)
        assert subscriber_count(channel) == 2
        assert await publish(channel, {"i": 1}) == 2
        assert await publish(channel, {"i": 2}) == 2
    assert received_a == received_b == [{"i": 1}, {"i": 2}]
    assert subscriber_count(channel) == 0


@pytest.mark.asyncio
async def test_channel_isolation_no_replay_and_aclose_cleanup(bound_cache) -> None:
    channel = f"cache-isolation:{uuid.uuid4()}"
    assert await publish(channel, {"old": True}) == 0
    ready = asyncio.Event()
    async with aclosing(subscribe(channel, on_subscribed=ready)) as events:
        pending = asyncio.create_task(anext(events))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            assert await publish(channel + ":other", {"wrong": True}) == 0
            assert await publish(channel, {"new": True}) == 1
            assert await asyncio.wait_for(pending, 2) == {"new": True}
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert subscriber_count(channel) == 0


@pytest.mark.asyncio
async def test_cancellation_balances_counts(bound_cache) -> None:
    channel = f"cache-cancel:{uuid.uuid4()}"
    ready = asyncio.Event()
    async with aclosing(subscribe(channel, on_subscribed=ready)) as events:
        pending = asyncio.create_task(anext(events))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            assert subscriber_count(channel) == 1
        finally:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    assert subscriber_count(channel) == 0


@pytest.mark.asyncio
async def test_malformed_payloads_are_skipped(redis_or_skip) -> None:
    provider = _RedisProvider()
    channel = f"cache-malformed:{uuid.uuid4()}"
    ready = asyncio.Event()
    with set_cache_for_tests(provider=provider):
        try:
            async with aclosing(subscribe(channel, on_subscribed=ready)) as events:
                pending = asyncio.create_task(anext(events))
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                    for payload in (b"not-json", b"\xff", b"[]", b"null"):
                        await provider._get_client().publish(channel, payload)
                    await publish(channel, {"valid": True})
                    assert await asyncio.wait_for(pending, 2) == {"valid": True}
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            assert subscriber_count(channel) == 0
        finally:
            await provider.shutdown()


@pytest.mark.asyncio
async def test_counts_are_local_to_provider_not_cluster(redis_or_skip) -> None:
    local, other = _RedisProvider(), _RedisProvider()
    channel = f"cache-local-count:{uuid.uuid4()}"
    ready = asyncio.Event()
    try:
        with set_cache_for_tests(provider=local):
            async with aclosing(subscribe(channel, on_subscribed=ready)) as events:
                pending = asyncio.create_task(anext(events))
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                    assert subscriber_count(channel) == 1
                    with set_cache_for_tests(provider=other):
                        assert subscriber_count(channel) == 0
                        assert await publish(channel, {"shared": True}) == 1
                    assert await asyncio.wait_for(pending, 2) == {"shared": True}
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            assert subscriber_count(channel) == 0
    finally:
        await local.shutdown()
        await other.shutdown()


@pytest.mark.asyncio
async def test_subscribe_is_synchronous_factory_and_captures_its_provider() -> None:
    assert not inspect.iscoroutinefunction(subscribe)
    ready = asyncio.Event()
    with set_cache_for_tests(scenario="memory") as original:
        iterator = subscribe("factory", on_subscribed=ready)
        assert inspect.isasyncgen(iterator)
    with set_cache_for_tests(scenario="memory"):
        async with aclosing(iterator) as events:
            pending = asyncio.create_task(anext(events))
            try:
                await asyncio.wait_for(ready.wait(), 2)
                assert subscriber_count("factory") == 0
                assert await original.publish("factory", {"original": True}) == 1
                assert await asyncio.wait_for(pending, 2) == {"original": True}
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert original.subscriber_count("factory") == 0
