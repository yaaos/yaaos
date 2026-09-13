"""Provider-neutral cache behaviors, input validation, and scoped substitution."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from typing import Any, Literal

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from app.core.cache.memory import _MemoryProvider
from app.core.cache.pubsub import _RedisProvider
from app.core.cache.recording import _RecordingProvider
from app.core.cache.types import CacheError, CacheValidationError, Membership, _event_payload, _Provider

log = structlog.get_logger("core.cache")

# Production shares one lazy, loop-safe provider. Contexts store only overrides;
# an immutable sentinel keeps default resource ownership outside ContextVar state.
_default_provider: _Provider = _RedisProvider()
_provider_var: ContextVar[_Provider | None] = ContextVar("_cache_provider", default=None)


def _get() -> _Provider:
    provider = _provider_var.get()
    return _default_provider if provider is None else provider


@contextmanager
def _operation() -> Iterator[None]:
    try:
        yield
    except Exception:
        # Neither the vendor message nor its chained traceback may escape.
        raise CacheError("Cache operation failed") from None


def _positive_integer(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CacheValidationError("Expected a positive integer")


def _timestamp(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CacheValidationError("Expected a finite nonnegative timestamp")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise CacheValidationError("Expected a finite nonnegative timestamp")


def _record_fields(fields: BaseModel) -> dict[str, str]:
    if not isinstance(fields, BaseModel):
        raise CacheValidationError("Record fields must be a typed model")
    try:
        values = fields.model_dump(mode="json", exclude_unset=True)
    except Exception:
        raise CacheValidationError("Record fields must be flat strings") from None
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()
    ):
        raise CacheValidationError("Record fields must be flat strings")
    return values


@contextmanager
def set_cache_for_tests(
    *,
    provider: _Provider | None = None,
    scenario: Literal["default", "memory", "recording"] = "default",
) -> Iterator[_Provider | _RecordingProvider]:
    """Bind a scoped provider; recording decorates the bound provider's storage.

    Scopes restore bindings, not external data. Call shutdown before leaving
    a scope when its provider has opened resources.
    """
    if scenario not in ("default", "memory", "recording"):
        raise CacheValidationError("Unknown cache test scenario")
    if provider is not None and scenario == "memory":
        raise CacheValidationError("Choose a provider or a memory scenario")
    instance: _Provider
    if scenario == "recording":
        instance = _RecordingProvider(provider if provider is not None else _get())
    elif provider is not None:
        instance = provider
    elif scenario == "memory":
        instance = _MemoryProvider()
    else:
        instance = _RedisProvider()
    binding = _provider_var.set(instance)
    try:
        yield instance
    finally:
        _provider_var.reset(binding)


async def read_record(key: str) -> dict[str, str]:
    with _operation():
        return await _get().read_record(key)


async def update_record(key: str, *, fields: BaseModel) -> None:
    values = _record_fields(fields)
    if values:
        with _operation():
            await _get().update_record(key, fields=values)


async def delete_record(key: str) -> None:
    with _operation():
        await _get().delete_record(key)


async def touch_member(key: str, member: str, *, last_seen: float) -> None:
    _timestamp(last_seen)
    with _operation():
        await _get().touch_member(key, member, last_seen=last_seen)


async def remove_member(key: str, member: str) -> bool:
    with _operation():
        return await _get().remove_member(key, member)


async def list_members(key: str) -> list[Membership]:
    with _operation():
        return await _get().list_members(key)


async def count_members(key: str) -> int:
    with _operation():
        return await _get().count_members(key)


async def prune_members(key: str, *, cutoff: float) -> int:
    _timestamp(cutoff)
    with _operation():
        return await _get().prune_members(key, cutoff=cutoff)


async def list_keys(*, prefix: str) -> list[str]:
    with _operation():
        return await _get().list_keys(prefix=prefix)


async def clear_prefix(prefix: str) -> int:
    with _operation():
        return await _get().clear_prefix(prefix)


async def claim_once(key: str, *, ttl_seconds: int) -> bool:
    _positive_integer(ttl_seconds)
    with _operation():
        return await _get().claim_once(key, ttl_seconds=ttl_seconds)


async def allow_hit(key: str, *, limit: int, window_seconds: int) -> bool:
    _positive_integer(limit)
    _positive_integer(window_seconds)
    with _operation():
        return await _get().allow_hit(key, limit=limit, window_seconds=window_seconds)


async def publish(channel: str, event: dict[str, Any]) -> int:
    _event_payload(event)
    with _operation():
        return await _get().publish(channel, event)


async def _subscribe(
    provider: _Provider, channel: str, on_subscribed: asyncio.Event | None
) -> AsyncGenerator[dict[str, Any]]:
    with _operation():
        async with aclosing(provider.subscribe(channel, on_subscribed=on_subscribed)) as events:
            async for event in events:
                yield event


def subscribe(channel: str, on_subscribed: asyncio.Event | None = None) -> AsyncIterator[dict[str, Any]]:
    """Capture the bound provider now; open the subscription on first iteration."""
    return _subscribe(_get(), channel, on_subscribed)


def subscriber_count(channel: str) -> int:
    """Process-local diagnostics, not a distributed subscriber census."""
    with _operation():
        return _get().subscriber_count(channel)


async def is_healthy() -> bool:
    try:
        healthy = await _get().is_healthy()
    except Exception:
        healthy = False
    # Report only a fresh neutral error outside the vendor exception context.
    if not healthy:
        span = trace.get_current_span()
        span.record_exception(CacheError("Cache health check failed"))
        span.set_status(StatusCode.ERROR, "cache_health_failed")
        log.warning("cache.health_failed")
    return healthy
