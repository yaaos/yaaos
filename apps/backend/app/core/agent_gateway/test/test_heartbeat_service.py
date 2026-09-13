"""Service-tier test for SubscriberRegistry.heartbeat().

heartbeat() refreshes last-seen times for run subscribers and agent routes
so the sweeper doesn't evict healthy long-lived subscribers.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4, uuid7

import pytest

from app.core.agent_gateway.subscribers import (
    _SUBSCRIBER_STALE_THRESHOLD_SECONDS,
    SubscriberRegistry,
    _agent_routes_key,
    _run_subscribers_key,
)
from app.core.cache import (
    count_members,
    list_members,
    prune_members,
    remove_member,
    touch_member,
)

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_prevents_stale_eviction(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """Heartbeat refreshes both memberships without adding members or publishing."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    run_id = uuid4()
    workspace_id = uuid7()

    sent: list[dict] = []
    received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        received.set()

    await reg.register_sender(agent_id, _sender)

    conn_id = await reg.track(
        run_id=run_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    run_key = _run_subscribers_key(run_id)
    routes_key = _agent_routes_key(agent_id)
    pod_member = f"{reg._pod_id}:{conn_id}"

    await asyncio.wait_for(received.wait(), timeout=2.0)
    assert len(sent) == 1

    # The inclusive stale cutoff removes exactly the backdated membership.
    cutoff = time.time() - _SUBSCRIBER_STALE_THRESHOLD_SECONDS
    for key, member in ((run_key, pod_member), (routes_key, str(run_id))):
        await touch_member(key, member, last_seen=cutoff)
        memberships = await list_members(key)
        assert [(entry.member, entry.last_seen) for entry in memberships] == [(member, cutoff)]
        assert await prune_members(key, cutoff=cutoff - 1) == 0
        assert await prune_members(key, cutoff=cutoff) == 1
        assert await count_members(key) == 0
        await touch_member(key, member, last_seen=cutoff)

    before = time.time()
    await reg.heartbeat(run_id=run_id, conn_id=conn_id, agent_id=agent_id)
    after = time.time()

    for key, member in ((run_key, pod_member), (routes_key, str(run_id))):
        memberships = await list_members(key)
        assert len(memberships) == 1
        assert memberships[0].member == member
        assert before <= memberships[0].last_seen <= after
        assert await prune_members(key, cutoff=cutoff) == 0
        assert await count_members(key) == 1

    await asyncio.sleep(0.05)
    assert len(sent) == 1, "heartbeat must not publish to the agent control channel"

    # Cleanup.
    reg.unregister_sender(agent_id)
    await reg.untrack(run_id=run_id, conn_id=conn_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_refreshes_both_memberships(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """Refreshing an existing membership leaves both counts unchanged."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    run_id = uuid4()
    workspace_id = uuid7()

    async def _sender(msg: dict) -> None:
        pass

    await reg.register_sender(agent_id, _sender)

    conn_id = await reg.track(
        run_id=run_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    run_key = _run_subscribers_key(run_id)
    routes_key = _agent_routes_key(agent_id)

    assert await count_members(run_key) == 1
    assert await count_members(routes_key) == 1

    # Call heartbeat multiple times — cardinality must not grow.
    await reg.heartbeat(run_id=run_id, conn_id=conn_id, agent_id=agent_id)
    await reg.heartbeat(run_id=run_id, conn_id=conn_id, agent_id=agent_id)

    assert await count_members(run_key) == 1
    assert await count_members(routes_key) == 1

    # Cleanup.
    reg.unregister_sender(agent_id)
    await reg.untrack(run_id=run_id, conn_id=conn_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_with_no_prior_track_does_not_raise(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """heartbeat() with a conn_id that was never tracked does not raise.

    Touching a nonexistent member creates it.
    The sweeper will later evict the phantom entry.
    """
    reg = SubscriberRegistry()
    agent_id = uuid4()
    run_id = uuid4()
    phantom_conn = "no-such-conn"

    # Should not raise even with no prior track().
    await reg.heartbeat(
        run_id=run_id,
        conn_id=phantom_conn,
        agent_id=agent_id,
    )
    # Clean up phantom entries.
    await remove_member(_run_subscribers_key(run_id), f"{reg._pod_id}:{phantom_conn}")
    await remove_member(_agent_routes_key(agent_id), str(run_id))
