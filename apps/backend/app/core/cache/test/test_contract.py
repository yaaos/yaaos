"""Provider-neutral storage, coordination, validation, and failure contract."""

from __future__ import annotations

import asyncio
import dataclasses
import traceback
import uuid
from functools import partial
from typing import Any

import pytest
from pydantic import BaseModel, RootModel

from app.core.cache import (
    CacheError,
    CacheValidationError,
    Membership,
    allow_hit,
    claim_once,
    clear_prefix,
    count_members,
    delete_record,
    is_healthy,
    list_keys,
    list_members,
    prune_members,
    read_record,
    remove_member,
    set_cache_for_tests,
    shutdown,
    touch_member,
    update_record,
)
from app.core.cache.memory import _MemoryProvider
from app.core.cache.pubsub import _RedisProvider


class _Record(BaseModel):
    first: str = "default"
    second: str = "default"


@pytest.mark.asyncio
async def test_provider_is_healthy(bound_cache) -> None:
    assert await is_healthy() is True


@pytest.mark.asyncio
async def test_records_partial_missing_and_empty_patch(bound_cache) -> None:
    key = f"cache-record:{uuid.uuid4()}"
    try:
        assert await read_record(key) == {}
        await delete_record(key)
        await update_record(key, fields=_Record())
        assert await list_keys(prefix=key) == []
        await update_record(key, fields=_Record(first="one", second="two"))
        await update_record(key, fields=_Record(first="changed"))
        result = await read_record(key)
        assert result == {"first": "changed", "second": "two"}
        result["second"] = "not stored"
        assert await read_record(key) == {"first": "changed", "second": "two"}
        await delete_record(key)
        assert await read_record(key) == {}
    finally:
        await delete_record(key)


@pytest.mark.asyncio
async def test_membership_update_count_remove_and_inclusive_cutoff(bound_cache) -> None:
    key = f"cache-members:{uuid.uuid4()}"
    try:
        assert await list_members(key) == []
        assert await count_members(key) == 0
        assert await remove_member(key, "missing") is False
        assert await prune_members(key, cutoff=0) == 0
        await touch_member(key, "old", last_seen=0)
        await touch_member(key, "boundary", last_seen=10)
        await touch_member(key, "fresh", last_seen=11)
        await touch_member(key, "fresh", last_seen=12)
        assert await count_members(key) == 3
        assert await list_members(key) == [
            Membership("old", 0),
            Membership("boundary", 10),
            Membership("fresh", 12),
        ]
        assert await prune_members(key, cutoff=10) == 2
        assert await list_members(key) == [Membership("fresh", 12)]
        assert await remove_member(key, "fresh") is True
        assert await remove_member(key, "fresh") is False
        assert await count_members(key) == 0
    finally:
        await clear_prefix(key)


def test_membership_is_immutable() -> None:
    member = Membership("member", 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        member.last_seen = 2


@pytest.mark.parametrize("suffix", ["*", "?", "[ab]", "\\", "[?]*\\"])
@pytest.mark.asyncio
async def test_prefix_is_literal_and_never_deletes_neighbors(bound_cache, suffix) -> None:
    root = f"cache-prefix:{uuid.uuid4()}:"
    prefix = root + suffix
    try:
        await update_record(prefix, fields=_Record(first="exact"))
        await update_record(prefix + ":child", fields=_Record(first="child"))
        await update_record(root + "unrelated", fields=_Record(first="neighbor"))
        assert set(await list_keys(prefix=prefix)) == {prefix, prefix + ":child"}
        assert await clear_prefix(prefix) == 2
        assert await clear_prefix(prefix) == 0
        assert await read_record(root + "unrelated") == {"first": "neighbor"}
    finally:
        await clear_prefix(root)


@pytest.mark.asyncio
async def test_wrong_type_is_error_not_miss_or_success(bound_cache) -> None:
    key = f"cache-wrong-type:{uuid.uuid4()}"
    try:
        await update_record(key, fields=_Record(first="record"))
        operations = [
            partial(touch_member, key, "member", last_seen=1),
            partial(list_members, key),
            partial(count_members, key),
            partial(remove_member, key, "member"),
            partial(prune_members, key, cutoff=1),
            partial(allow_hit, key, limit=1, window_seconds=1),
        ]
        for operation in operations:
            with pytest.raises(CacheError):
                await operation()
        await delete_record(key)
        await touch_member(key, "member", last_seen=1)
        with pytest.raises(CacheError):
            await read_record(key)
        with pytest.raises(CacheError):
            await update_record(key, fields=_Record(first="not-a-record"))
    finally:
        await clear_prefix(key)


@pytest.mark.asyncio
async def test_claim_duplicates_and_expiry(bound_cache) -> None:
    key = f"cache-claim:{uuid.uuid4()}"
    try:
        assert await claim_once(key, ttl_seconds=1) is True
        assert await claim_once(key, ttl_seconds=60) is False
        await asyncio.sleep(1.1)
        assert await claim_once(key, ttl_seconds=1) is True
    finally:
        await clear_prefix(key)


@pytest.mark.asyncio
async def test_atomic_claim_has_one_winner_across_independent_contexts(redis_or_skip) -> None:
    key = f"cache-atomic-claim:{uuid.uuid4()}"
    providers = [_RedisProvider(), _RedisProvider()]

    async def attempt(provider: _RedisProvider) -> bool:
        with set_cache_for_tests(provider=provider):
            return await claim_once(key, ttl_seconds=60)

    try:
        results = await asyncio.gather(*(attempt(providers[i % 2]) for i in range(32)))
        assert sum(results) == 1
    finally:
        with set_cache_for_tests(provider=providers[0]):
            await clear_prefix(key)
        for provider in providers:
            await provider.shutdown()


class _NoIOProvider(_MemoryProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def update_record(self, key: str, *, fields: dict[str, str]) -> None:
        self.calls += 1
        raise AssertionError("unexpected I/O")

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        self.calls += 1
        raise AssertionError("unexpected I/O")

    async def allow_hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        self.calls += 1
        raise AssertionError("unexpected I/O")

    async def touch_member(self, key: str, member: str, *, last_seen: float) -> None:
        self.calls += 1
        raise AssertionError("unexpected I/O")

    async def prune_members(self, key: str, *, cutoff: float) -> int:
        self.calls += 1
        raise AssertionError("unexpected I/O")


@pytest.mark.parametrize("invalid", [0, -1, True, False, 1.5, "1", None])
@pytest.mark.asyncio
async def test_positive_integer_validation_before_io(invalid) -> None:
    provider = _NoIOProvider()
    with set_cache_for_tests(provider=provider):
        with pytest.raises(CacheValidationError):
            await claim_once("key", ttl_seconds=invalid)
        with pytest.raises(CacheValidationError):
            await allow_hit("key", limit=invalid, window_seconds=1)
        with pytest.raises(CacheValidationError):
            await allow_hit("key", limit=1, window_seconds=invalid)
    assert provider.calls == 0


@pytest.mark.parametrize("invalid", [-1, float("nan"), float("inf"), -float("inf"), True, "1", None])
@pytest.mark.asyncio
async def test_timestamp_validation_before_io(invalid) -> None:
    provider = _NoIOProvider()
    with set_cache_for_tests(provider=provider):
        with pytest.raises(CacheValidationError):
            await touch_member("key", "member", last_seen=invalid)
        with pytest.raises(CacheValidationError):
            await prune_members("key", cutoff=invalid)
    assert provider.calls == 0


@pytest.mark.parametrize("value", [1, None, False, ["value"], {"nested": "value"}])
@pytest.mark.asyncio
async def test_record_validation_before_io(value: Any) -> None:
    provider = _NoIOProvider()
    with set_cache_for_tests(provider=provider):
        with pytest.raises(CacheValidationError):
            await update_record("key", fields=RootModel[dict[str, Any]]({"field": value}))
        await update_record("key", fields=_Record())
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_untyped_or_nonmapping_record_patch_is_rejected_before_io() -> None:
    provider = _NoIOProvider()
    with set_cache_for_tests(provider=provider):
        with pytest.raises(CacheValidationError):
            await update_record("key", fields={"field": "untyped"})
        with pytest.raises(CacheValidationError):
            await update_record("key", fields=RootModel[list[str]](["not-a-record"]))
    assert provider.calls == 0


class _BrokenProvider(_MemoryProvider):
    async def read_record(self, key: str) -> dict[str, str]:
        raise RuntimeError("provider-private-detail")

    async def is_healthy(self) -> bool:
        raise RuntimeError("provider-private-detail")

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        raise RuntimeError("provider-private-detail")


@pytest.mark.asyncio
async def test_operational_failures_are_sanitized_and_health_is_false() -> None:
    with set_cache_for_tests(provider=_BrokenProvider()):
        with pytest.raises(CacheError) as caught:
            await read_record("sensitive-key")
        rendered = "".join(traceback.format_exception(caught.value))
        assert "provider-private-detail" not in rendered
        assert "sensitive-key" not in str(caught.value)
        assert caught.value.__suppress_context__
        with pytest.raises(CacheError):
            await claim_once("sensitive-key", ttl_seconds=1)
        assert await is_healthy() is False
        await shutdown()
