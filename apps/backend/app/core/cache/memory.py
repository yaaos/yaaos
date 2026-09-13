"""Private in-memory contract implementation for isolated tests, never a fallback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from app.core.cache.types import CacheError, Membership, _decode_event, _event_payload


class _MemoryProvider:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._records: dict[str, dict[str, str]] = {}
        self._members: dict[str, dict[str, float]] = {}
        self._claims: dict[str, str] = {}
        self._expires: dict[str, float] = {}
        self._channels: dict[str, set[asyncio.Queue[str | None]]] = {}

    def _delete(self, key: str) -> bool:
        existed = key in self._records or key in self._members or key in self._claims
        self._records.pop(key, None)
        self._members.pop(key, None)
        self._claims.pop(key, None)
        self._expires.pop(key, None)
        return existed

    def _expire(self, key: str) -> None:
        expiry = self._expires.get(key)
        if expiry is not None and expiry <= self._clock():
            self._delete(key)

    def _record(self, key: str) -> dict[str, str]:
        self._expire(key)
        if key in self._members or key in self._claims:
            raise CacheError("Incompatible cache value")
        return self._records.get(key, {})

    def _membership(self, key: str) -> dict[str, float]:
        self._expire(key)
        if key in self._records or key in self._claims:
            raise CacheError("Incompatible cache value")
        return self._members.get(key, {})

    async def read_record(self, key: str) -> dict[str, str]:
        return self._record(key).copy()

    async def update_record(self, key: str, *, fields: dict[str, str]) -> None:
        if fields:
            record = self._record(key)
            record.update(fields)
            self._records[key] = record

    async def delete_record(self, key: str) -> None:
        self._delete(key)

    async def touch_member(self, key: str, member: str, *, last_seen: float) -> None:
        members = self._membership(key)
        members[member] = last_seen
        self._members[key] = members

    async def remove_member(self, key: str, member: str) -> bool:
        members = self._membership(key)
        existed = member in members
        members.pop(member, None)
        if not members:
            self._delete(key)
        return existed

    async def list_members(self, key: str) -> list[Membership]:
        return [
            Membership(member, timestamp)
            for member, timestamp in sorted(
                self._membership(key).items(), key=lambda item: (item[1], item[0])
            )
        ]

    async def count_members(self, key: str) -> int:
        return len(self._membership(key))

    async def prune_members(self, key: str, *, cutoff: float) -> int:
        members = self._membership(key)
        stale = [member for member, timestamp in members.items() if timestamp <= cutoff]
        for member in stale:
            del members[member]
        if not members:
            self._delete(key)
        return len(stale)

    async def list_keys(self, *, prefix: str) -> list[str]:
        keys = self._records.keys() | self._members.keys() | self._claims.keys()
        for key in keys:
            self._expire(key)
        remaining = self._records.keys() | self._members.keys() | self._claims.keys()
        return sorted(key for key in remaining if key.startswith(prefix))

    async def clear_prefix(self, prefix: str) -> int:
        return sum(self._delete(key) for key in await self.list_keys(prefix=prefix))

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        self._expire(key)
        if key in self._records or key in self._members or key in self._claims:
            return False
        # No suspension between the existence check and insertion.
        self._claims[key] = "1"
        self._expires[key] = self._clock() + ttl_seconds
        return True

    async def allow_hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = self._clock()
        await self.prune_members(key, cutoff=now - window_seconds)
        count = await self.count_members(key)
        if count >= limit:
            return False
        await self.touch_member(key, f"{now:.6f}:{count}", last_seen=now)
        self._expires[key] = now + window_seconds
        return True

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        payload = _event_payload(event)
        queues = tuple(self._channels.get(channel, ()))
        for queue in queues:
            queue.put_nowait(payload)
        return len(queues)

    async def subscribe(
        self, channel: str, on_subscribed: asyncio.Event | None = None
    ) -> AsyncGenerator[dict[str, Any]]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        queues = self._channels.setdefault(channel, set())
        queues.add(queue)
        try:
            if on_subscribed is not None:
                on_subscribed.set()
            while (payload := await queue.get()) is not None:
                event = _decode_event(payload)
                if event is not None:
                    yield event
        finally:
            queues.discard(queue)
            if not queues and self._channels.get(channel) is queues:
                self._channels.pop(channel, None)

    def subscriber_count(self, channel: str) -> int:
        return len(self._channels.get(channel, ()))

    async def is_healthy(self) -> bool:
        return True

    async def shutdown(self) -> None:
        channels = tuple(self._channels.values())
        self._channels.clear()
        for queues in channels:
            for queue in queues:
                queue.put_nowait(None)
