"""Test-only event recorder that preserves the bound provider's other behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from app.core.cache.types import Membership, _event_payload, _Provider


class _RecordingProvider:
    def __init__(self, provider: _Provider) -> None:
        self._provider = provider
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def read_record(self, key: str) -> dict[str, str]:
        return await self._provider.read_record(key)

    async def update_record(self, key: str, *, fields: dict[str, str]) -> None:
        await self._provider.update_record(key, fields=fields)

    async def delete_record(self, key: str) -> None:
        await self._provider.delete_record(key)

    async def touch_member(self, key: str, member: str, *, last_seen: float) -> None:
        await self._provider.touch_member(key, member, last_seen=last_seen)

    async def remove_member(self, key: str, member: str) -> bool:
        return await self._provider.remove_member(key, member)

    async def list_members(self, key: str) -> list[Membership]:
        return await self._provider.list_members(key)

    async def count_members(self, key: str) -> int:
        return await self._provider.count_members(key)

    async def prune_members(self, key: str, *, cutoff: float) -> int:
        return await self._provider.prune_members(key, cutoff=cutoff)

    async def list_keys(self, *, prefix: str) -> list[str]:
        return await self._provider.list_keys(prefix=prefix)

    async def clear_prefix(self, prefix: str) -> int:
        return await self._provider.clear_prefix(prefix)

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        return await self._provider.claim_once(key, ttl_seconds=ttl_seconds)

    async def allow_hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        return await self._provider.allow_hit(key, limit=limit, window_seconds=window_seconds)

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        self.published.append((channel, json.loads(_event_payload(event))))
        return 0

    def subscribe(
        self, channel: str, on_subscribed: asyncio.Event | None = None
    ) -> AsyncGenerator[dict[str, Any]]:
        return self._provider.subscribe(channel, on_subscribed=on_subscribed)

    def subscriber_count(self, channel: str) -> int:
        return self._provider.subscriber_count(channel)

    async def is_healthy(self) -> bool:
        return await self._provider.is_healthy()

    async def shutdown(self) -> None:
        await self._provider.shutdown()
