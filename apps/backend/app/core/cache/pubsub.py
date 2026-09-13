"""Private Redis provider, loop-bound connections, and acknowledged JSON pub/sub."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import aclosing
from typing import Any

import structlog
from redis.asyncio import Redis, from_url
from redis.asyncio.client import PubSub

from app.core.cache.hash_ops import _read_record, _update_record
from app.core.cache.set_if_absent import _claim_once
from app.core.cache.set_ops import _clear_prefix, _list_keys
from app.core.cache.sliding_window import _allow_hit
from app.core.cache.types import CacheError, Membership, _decode_event, _event_payload
from app.core.cache.zset_ops import _list_members
from app.core.config import get_settings

log = structlog.get_logger("core.cache.provider")


async def _close_on_loop(loop: asyncio.AbstractEventLoop, operation: Coroutine[Any, Any, None]) -> None:
    if loop is not asyncio.get_running_loop() and loop.is_running():
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(operation, loop))
    else:
        await operation


class _RedisProvider:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._clients: dict[asyncio.AbstractEventLoop, Redis] = {}
        self._subscriptions: dict[PubSub, tuple[asyncio.AbstractEventLoop, str]] = {}
        self._local_members: dict[str, set[PubSub]] = {}

    def _get_client(self) -> Redis:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = from_url(get_settings().redis_url, decode_responses=False)
            self._clients[loop] = client
        return client

    async def read_record(self, key: str) -> dict[str, str]:
        return await _read_record(self._get_client(), key)

    async def update_record(self, key: str, *, fields: dict[str, str]) -> None:
        await _update_record(self._get_client(), key, fields=fields)

    async def delete_record(self, key: str) -> None:
        await self._get_client().delete(key)

    async def touch_member(self, key: str, member: str, *, last_seen: float) -> None:
        await self._get_client().zadd(key, {member: last_seen})

    async def remove_member(self, key: str, member: str) -> bool:
        return bool(await self._get_client().zrem(key, member))

    async def list_members(self, key: str) -> list[Membership]:
        return await _list_members(self._get_client(), key)

    async def count_members(self, key: str) -> int:
        return int(await self._get_client().zcard(key))

    async def prune_members(self, key: str, *, cutoff: float) -> int:
        return int(await self._get_client().zremrangebyscore(key, "-inf", cutoff))

    async def list_keys(self, *, prefix: str) -> list[str]:
        return await _list_keys(self._get_client(), prefix=prefix)

    async def clear_prefix(self, prefix: str) -> int:
        return await _clear_prefix(self._get_client(), prefix)

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        return await _claim_once(self._get_client(), key, ttl_seconds=ttl_seconds)

    async def allow_hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        return await _allow_hit(
            self._get_client(), key, limit=limit, window_seconds=window_seconds, now=self._clock()
        )

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        return int(await self._get_client().publish(channel, _event_payload(event).encode()))

    async def _close_subscription(self, subscription: PubSub) -> None:
        registration = self._subscriptions.pop(subscription, None)
        if registration is None:
            return
        _, channel = registration
        members = self._local_members.get(channel)
        if members is not None:
            members.discard(subscription)
            if not members:
                self._local_members.pop(channel, None)
        try:
            await subscription.unsubscribe(channel)
        finally:
            await subscription.aclose()

    async def subscribe(
        self, channel: str, on_subscribed: asyncio.Event | None = None
    ) -> AsyncGenerator[dict[str, Any]]:
        subscription = self._get_client().pubsub()
        self._subscriptions[subscription] = (asyncio.get_running_loop(), channel)
        try:
            # subscribe() only sends the command; the confirmation is the barrier.
            await subscription.subscribe(channel)
            async with aclosing(subscription.listen()) as messages:
                async for message in messages:
                    kind = message.get("type")
                    if kind in ("subscribe", b"subscribe") and message.get("channel") in (
                        channel,
                        channel.encode(),
                    ):
                        self._local_members.setdefault(channel, set()).add(subscription)
                        if on_subscribed is not None:
                            on_subscribed.set()
                    elif kind in ("message", b"message"):
                        payload = message.get("data")
                        if not isinstance(payload, (str, bytes)):
                            continue
                        event = _decode_event(payload)
                        if event is None:
                            log.warning("cache.malformed_event")
                            continue
                        yield event
        finally:
            await self._close_subscription(subscription)

    def subscriber_count(self, channel: str) -> int:
        return len(self._local_members.get(channel, ()))

    async def is_healthy(self) -> bool:
        return bool(await self._get_client().ping())

    async def shutdown(self) -> None:
        # Drain every resource even when one close fails, without exposing its error.
        failed = False
        for subscription, (loop, _) in tuple(self._subscriptions.items()):
            try:
                await _close_on_loop(loop, self._close_subscription(subscription))
            except Exception:
                # A closed owner loop cannot complete transport callbacks. Its
                # registrations are already removed; preserve best-effort teardown.
                failed = failed or not loop.is_closed()
        clients = tuple(self._clients.items())
        self._clients.clear()
        for loop, client in clients:
            try:
                await _close_on_loop(loop, client.aclose())
            except Exception:
                failed = failed or not loop.is_closed()
        if failed:
            raise CacheError("Cache shutdown failed") from None
