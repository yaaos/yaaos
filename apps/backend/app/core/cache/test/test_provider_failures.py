"""Private failure injection proves readiness and cleanup before any publication."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any, cast

import pytest
from redis.asyncio import Redis

from app.core.cache import CacheError, list_keys, set_cache_for_tests, subscribe, subscriber_count
from app.core.cache.pubsub import _RedisProvider


class _Subscription:
    def __init__(self, *, fail_setup: bool = False) -> None:
        self.started = asyncio.Event()
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.fail_setup = fail_setup
        self.unsubscribed = 0
        self.closed = 0
        self.listen_closed = False

    async def subscribe(self, channel: str) -> None:
        self.started.set()
        if self.fail_setup:
            raise RuntimeError("provider-private-detail")

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                yield await self.messages.get()
        finally:
            self.listen_closed = True

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed += 1

    async def aclose(self) -> None:
        self.closed += 1


class _Client:
    def __init__(self, subscription: _Subscription) -> None:
        self.subscription = subscription
        self.patterns: list[str] = []

    def pubsub(self) -> _Subscription:
        return self.subscription

    async def scan_iter(self, *, match: str, count: int) -> AsyncIterator[bytes]:
        self.patterns.append(match)
        for key in (b"prefix[*]:key", b"unrelated", b"prefix[*]:key"):
            yield key


class _Provider(_RedisProvider):
    def __init__(self, subscription: _Subscription) -> None:
        super().__init__()
        self.client = _Client(subscription)

    def _get_client(self) -> Redis:
        return cast(Redis, self.client)


@pytest.mark.asyncio
async def test_readiness_waits_for_confirmation_and_closes_nested_iterator() -> None:
    subscription = _Subscription()
    ready = asyncio.Event()
    with set_cache_for_tests(provider=_Provider(subscription)):
        async with aclosing(subscribe("channel", on_subscribed=ready)) as events:
            pending = asyncio.create_task(anext(events))
            try:
                await asyncio.wait_for(subscription.started.wait(), 2)
                assert not ready.is_set()
                subscription.messages.put_nowait({"type": "subscribe", "channel": b"channel"})
                await asyncio.wait_for(ready.wait(), 2)
                assert subscriber_count("channel") == 1
                subscription.messages.put_nowait({"type": "message", "data": b'{"ready":true}'})
                assert await asyncio.wait_for(pending, 2) == {"ready": True}
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert subscriber_count("channel") == 0
    assert subscription.listen_closed
    assert subscription.unsubscribed == subscription.closed == 1


@pytest.mark.asyncio
async def test_subscription_setup_failure_is_sanitized_and_closed() -> None:
    subscription = _Subscription(fail_setup=True)
    ready = asyncio.Event()
    with set_cache_for_tests(provider=_Provider(subscription)):
        async with aclosing(subscribe("channel", on_subscribed=ready)) as events:
            with pytest.raises(CacheError) as caught:
                await anext(events)
        assert subscriber_count("channel") == 0
        assert not ready.is_set()
        assert "provider-private-detail" not in "".join(traceback.format_exception(caught.value))
    assert subscription.unsubscribed == subscription.closed == 1


@pytest.mark.asyncio
async def test_cancellation_before_confirmation_closes_resources() -> None:
    subscription = _Subscription()
    ready = asyncio.Event()
    with set_cache_for_tests(provider=_Provider(subscription)):
        async with aclosing(subscribe("channel", on_subscribed=ready)) as events:
            pending = asyncio.create_task(anext(events))
            await asyncio.wait_for(subscription.started.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert not ready.is_set()
        assert subscriber_count("channel") == 0
    assert subscription.listen_closed
    assert subscription.unsubscribed == subscription.closed == 1


@pytest.mark.asyncio
async def test_scan_escapes_deduplicates_and_filters_provider_results() -> None:
    provider = _Provider(_Subscription())
    with set_cache_for_tests(provider=provider):
        assert await list_keys(prefix="prefix[*]:") == ["prefix[*]:key"]
    assert provider.client.patterns == ["prefix\\[\\*\\]:*"]
