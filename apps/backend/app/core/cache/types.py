"""Cache vocabulary and the private, behavior-only provider contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Protocol


class CacheError(Exception):
    """A cache operation failed; messages never contain provider details or data."""


class CacheValidationError(ValueError):
    """Invalid cache input, rejected before provider I/O."""


@dataclass(frozen=True)
class Membership:
    member: str
    last_seen: float


class _Provider(Protocol):
    async def read_record(self, key: str) -> dict[str, str]: ...

    async def update_record(self, key: str, *, fields: dict[str, str]) -> None: ...

    async def delete_record(self, key: str) -> None: ...

    async def touch_member(self, key: str, member: str, *, last_seen: float) -> None: ...

    async def remove_member(self, key: str, member: str) -> bool: ...

    async def list_members(self, key: str) -> list[Membership]: ...

    async def count_members(self, key: str) -> int: ...

    async def prune_members(self, key: str, *, cutoff: float) -> int: ...

    async def list_keys(self, *, prefix: str) -> list[str]: ...

    async def clear_prefix(self, prefix: str) -> int: ...

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool: ...

    async def allow_hit(self, key: str, *, limit: int, window_seconds: int) -> bool: ...

    async def publish(self, channel: str, event: dict[str, Any]) -> int: ...

    def subscribe(
        self, channel: str, on_subscribed: asyncio.Event | None = None
    ) -> AsyncGenerator[dict[str, Any]]: ...

    def subscriber_count(self, channel: str) -> int: ...

    async def is_healthy(self) -> bool: ...

    async def shutdown(self) -> None: ...


def _event_payload(event: dict[str, Any]) -> str:
    if not isinstance(event, dict):
        raise CacheValidationError("Event must be a JSON object")
    try:
        return json.dumps(event)
    except TypeError, ValueError, OverflowError:
        raise CacheValidationError("Event must be JSON serializable") from None


def _decode_event(payload: str | bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(payload)
    except UnicodeError, json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None
