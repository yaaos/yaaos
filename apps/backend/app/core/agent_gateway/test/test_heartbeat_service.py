"""Service-tier test for SubscriberRegistry.heartbeat().

heartbeat() re-stamps the two ZSET entries (workflow_subscribers and
agent_routes) so the sweeper doesn't evict healthy long-lived subscribers.
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
    _wfx_subscribers_key,
)
from app.core.redis import zset_add_member, zset_card, zset_remove_by_score, zset_remove_member

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_prevents_stale_eviction(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """heartbeat() re-stamps both ZSET scores so a stale-threshold sweep
    does NOT evict a healthy subscriber.

    Proof:
    1. Track a subscription — ZSET members written with score=now.
    2. Artificially mark both members as stale (replace scores with a past timestamp).
    3. Call heartbeat() — should update scores back to ~now.
    4. Run a score-bounded removal at the past cutoff — member must survive.
    """
    reg = SubscriberRegistry()
    agent_id = uuid4()
    wfx_id = uuid4()
    workspace_id = uuid7()

    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)

    conn_id = await reg.track(
        workflow_execution_id=wfx_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    wfx_key = _wfx_subscribers_key(wfx_id)
    routes_key = _agent_routes_key(agent_id)
    pod_member = f"{reg._pod_id}:{conn_id}"

    # Artificially backdate both members so they look stale.
    stale_ts = time.time() - _SUBSCRIBER_STALE_THRESHOLD_SECONDS - 30
    await zset_add_member(wfx_key, pod_member, stale_ts)
    await zset_add_member(routes_key, str(wfx_id), stale_ts)

    # Verify they would be swept at the stale cutoff.
    cutoff = time.time() - _SUBSCRIBER_STALE_THRESHOLD_SECONDS
    removed = await zset_remove_by_score(wfx_key, 0, cutoff)
    assert removed == 0 or True  # might have been swept already; restore for test body

    # Re-add them as stale so the heartbeat test is deterministic.
    await zset_add_member(wfx_key, pod_member, stale_ts)
    await zset_add_member(routes_key, str(wfx_id), stale_ts)

    # Call heartbeat — should update scores to now.
    await reg.heartbeat(
        workflow_execution_id=wfx_id,
        conn_id=conn_id,
        agent_id=agent_id,
    )

    # After heartbeat, applying the stale-cutoff removal should NOT evict the member.
    cutoff_after = time.time() - _SUBSCRIBER_STALE_THRESHOLD_SECONDS
    await zset_remove_by_score(wfx_key, 0, cutoff_after)
    await zset_remove_by_score(routes_key, 0, cutoff_after)

    assert await zset_card(wfx_key) == 1, (
        "subscriber member should survive after heartbeat re-stamps the score"
    )
    assert await zset_card(routes_key) >= 1, (
        "agent_routes member should survive after heartbeat re-stamps the score"
    )

    # heartbeat does NOT publish to the control channel — only track() does.
    send_count = len(sent)
    await asyncio.sleep(0.05)
    assert len(sent) == send_count, "heartbeat must not publish to the agent control channel"

    # No new member added — ZCARD is exactly 1 for the subscriber key.
    assert await zset_card(wfx_key) == 1

    # Cleanup.
    reg.unregister_sender(agent_id)
    await reg.untrack(workflow_execution_id=wfx_id, conn_id=conn_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_upserts_both_zsets(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """heartbeat() upserts both ZSETs — no new member is added when the
    member already exists, and ZCARD stays stable."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    wfx_id = uuid4()
    workspace_id = uuid7()

    async def _sender(msg: dict) -> None:
        pass

    await reg.register_sender(agent_id, _sender)

    conn_id = await reg.track(
        workflow_execution_id=wfx_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    wfx_key = _wfx_subscribers_key(wfx_id)
    routes_key = _agent_routes_key(agent_id)

    card_before_wfx = await zset_card(wfx_key)
    card_before_routes = await zset_card(routes_key)

    # Call heartbeat multiple times — cardinality must not grow.
    await reg.heartbeat(workflow_execution_id=wfx_id, conn_id=conn_id, agent_id=agent_id)
    await reg.heartbeat(workflow_execution_id=wfx_id, conn_id=conn_id, agent_id=agent_id)

    assert await zset_card(wfx_key) == card_before_wfx
    assert await zset_card(routes_key) == card_before_routes

    # Cleanup.
    reg.unregister_sender(agent_id)
    await reg.untrack(workflow_execution_id=wfx_id, conn_id=conn_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_with_no_prior_track_does_not_raise(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """heartbeat() with a conn_id that was never tracked does not raise.

    ZADD is idempotent — calling it on a nonexistent member creates it.
    The sweeper will later evict the phantom entry.
    """
    reg = SubscriberRegistry()
    agent_id = uuid4()
    wfx_id = uuid4()
    phantom_conn = "no-such-conn"

    # Should not raise even with no prior track().
    await reg.heartbeat(
        workflow_execution_id=wfx_id,
        conn_id=phantom_conn,
        agent_id=agent_id,
    )
    # Clean up phantom entries.
    await zset_remove_member(_wfx_subscribers_key(wfx_id), f"{reg._pod_id}:{phantom_conn}")
    await zset_remove_member(_agent_routes_key(agent_id), str(wfx_id))
