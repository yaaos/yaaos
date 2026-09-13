"""Redis layouts stay interoperable with independent writers."""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel

from app.core.cache import (
    CacheError,
    Membership,
    allow_hit,
    claim_once,
    clear_prefix,
    list_members,
    read_record,
    set_cache_for_tests,
    touch_member,
    update_record,
)
from app.core.cache.pubsub import _RedisProvider


class _Route(BaseModel):
    workspace_id: str = ""
    agent_id: str = ""


@pytest.mark.asyncio
async def test_mixed_writers_keep_records_memberships_claims_and_windows(redis_or_skip) -> None:
    provider = _RedisProvider(clock=lambda: 100.25)
    root = f"cache-layout:{uuid.uuid4()}:"
    with set_cache_for_tests(provider=provider):
        client = provider._get_client()
        try:
            await client.hset(root + "route", mapping={"workspace_id": "workspace", "agent_id": "agent"})
            assert await read_record(root + "route") == {"workspace_id": "workspace", "agent_id": "agent"}
            await update_record(root + "route", fields=_Route(agent_id="new-agent"))
            assert await client.hgetall(root + "route") == {
                b"workspace_id": b"workspace",
                b"agent_id": b"new-agent",
            }
            await client.zadd(root + "presence", {"pod:connection": 10.0})
            assert await list_members(root + "presence") == [Membership("pod:connection", 10)]
            await touch_member(root + "presence", "pod:connection", last_seen=12)
            assert await client.zscore(root + "presence", "pod:connection") == 12
            assert await claim_once(root + "claim", ttl_seconds=60)
            assert await client.get(root + "claim") == b"1"
            await client.pexpire(root + "claim", 5000)
            assert not await claim_once(root + "claim", ttl_seconds=60)
            assert 0 < await client.pttl(root + "claim") <= 5000
            await client.set(root + "old-claim", "1", nx=True, ex=60)
            assert not await claim_once(root + "old-claim", ttl_seconds=60)
            await client.zadd(root + "window", {"99.000000:0": 99, "1.000000:0": 1})
            assert await allow_hit(root + "window", limit=2, window_seconds=10)
            assert await client.zrange(root + "window", 0, -1, withscores=True) == [
                (b"99.000000:0", 99),
                (b"100.250000:1", 100.25),
            ]
            await client.sadd(root + "incompatible-presence", "member")
            with pytest.raises(CacheError):
                await list_members(root + "incompatible-presence")
            assert await list_members(root + "presence") == [Membership("pod:connection", 12)]
        finally:
            await clear_prefix(root)
            await provider.shutdown()
