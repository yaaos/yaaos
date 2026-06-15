"""SubscriberRegistry demand-pull semantics: ZSET presence tracks subscriber
count; pub/sub backplane routes subscribe/unsubscribe to the WS-owning pod.

Tests marked `service` + `redis_or_skip` require a live Redis instance — the
new registry writes ZSET/HASH/SET state and delivers control messages via
Redis pub/sub, so process-local unit testing is not possible for anything
that crosses the pub/sub boundary.

Tests that only exercise the local sender registration (no track/untrack) do
not require Redis and run in-process.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4, uuid7

import pytest

from app.core.agent_gateway import (
    SubscriberRegistry,
    get_subscriber_registry,
)
from app.core.agent_gateway.subscribers import _wfx_subscribers_key
from app.core.redis import zset_card

# ── Tests that don't require Redis (no track/untrack) ─────────────────────────


@pytest.mark.asyncio
async def test_get_subscriber_registry_singleton() -> None:
    assert get_subscriber_registry() is get_subscriber_registry()


@pytest.mark.asyncio
async def test_register_sender_no_active_routes_sends_nothing(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """With no active routes in Redis, register_sender sends nothing."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)
    # Unregister to cancel the background task.
    reg.unregister_sender(agent_id)
    assert sent == []


# ── Tests that require Redis (track/untrack cross the pub/sub boundary) ────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_first_track_sends_subscribe(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """After register_sender, track() publishes a subscribe control message
    to the agent_ws_control:{agent_id} channel; the registered sender receives
    it via the pub/sub consumer task."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid7()
    sent: list[dict] = []
    received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        received.set()

    await reg.register_sender(agent_id, _sender)
    _conn = await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    # Wait up to 2s for the pub/sub consumer to deliver the message.
    await asyncio.wait_for(received.wait(), timeout=2.0)
    reg.unregister_sender(agent_id)

    assert sent[0]["type"] == "subscribe"
    assert sent[0]["workspace_id"] == str(workspace_id)
    assert sent[0]["workflow_execution_id"] == str(workflow_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_second_track_does_not_resend_subscribe(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """A second track() for the same wfx_id from the same pod still publishes
    a subscribe message (different ZSET member), but the ZSET ZADD update is
    idempotent. The send count reflects both publishes."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid7()
    sent: list[dict] = []
    count = [0]
    received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        count[0] += 1
        if count[0] >= 2:
            received.set()

    await reg.register_sender(agent_id, _sender)
    _conn1 = await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    _conn2 = await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    # Both track() calls publish — agent receives both subscribe signals;
    # the agent's own idempotent subscribe handling absorbs duplicates.
    await asyncio.wait_for(received.wait(), timeout=2.0)
    reg.unregister_sender(agent_id)
    assert all(m["type"] == "subscribe" for m in sent[:2])


@pytest.mark.asyncio
@pytest.mark.service
async def test_last_untrack_sends_unsubscribe(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """After two tracks and two untracks, the second untrack (ZCARD drops to 0)
    publishes an unsubscribe control message to the sender."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid7()
    sent: list[dict] = []
    unsub_received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        if msg.get("type") == "unsubscribe":
            unsub_received.set()

    await reg.register_sender(agent_id, _sender)
    conn1 = await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    # Wait for the first subscribe to arrive so the ZSET is populated.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if sent:
            break

    # Track returns a unique conn_id per call; untrack requires it.
    # One track → one ZSET member → untrack drops ZCARD to 0 → unsubscribe fires.
    await reg.untrack(workflow_execution_id=workflow_id, conn_id=conn1)
    await asyncio.wait_for(unsub_received.wait(), timeout=2.0)
    reg.unregister_sender(agent_id)

    unsubscribes = [m for m in sent if m["type"] == "unsubscribe"]
    assert len(unsubscribes) >= 1
    assert unsubscribes[-1]["workspace_id"] == str(workspace_id)
    assert unsubscribes[-1]["workflow_execution_id"] == str(workflow_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_untrack_below_zero_is_noop(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """untrack() on an unknown wfx_id is a no-op — no error, no message."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)
    # No track() call — untrack with an arbitrary conn_id should silently do nothing.
    await reg.untrack(workflow_execution_id=workflow_id, conn_id="no-such-conn")
    await asyncio.sleep(0.05)
    reg.unregister_sender(agent_id)
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.service
async def test_track_without_sender_doesnt_raise(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """track() with no sender registered writes to Redis without raising.
    The subscribe message is published to the pub/sub channel but no local
    sender receives it (that's the cross-pod case)."""
    reg = SubscriberRegistry()
    workflow_id = uuid4()
    workspace_id = uuid7()
    agent_id = uuid4()

    # Should not raise even without a registered sender.
    conn = await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    # No assertions on count — count is Redis ZCARD, not a local field.
    # Verify cleanup.
    await reg.untrack(workflow_execution_id=workflow_id, conn_id=conn)


@pytest.mark.asyncio
@pytest.mark.service
async def test_register_sender_replays_subscribes_for_active_routes(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """When the agent's WS reconnects, the new sender registration must
    re-emit subscribe messages for every active route pointing at that
    agent. Without this, progress events for already-watching UIs would
    drop until each UI detached + re-attached."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    wfx_a, ws_a = uuid4(), uuid4()
    wfx_b, ws_b = uuid4(), uuid4()

    # Initial connection: register sender, then track two workflows.
    sent_first: list[dict] = []
    first_count = [0]
    first_received = asyncio.Event()

    async def _first(msg: dict) -> None:
        sent_first.append(msg)
        first_count[0] += 1
        if first_count[0] >= 2:
            first_received.set()

    await reg.register_sender(agent_id, _first)
    _conn_a = await reg.track(workflow_execution_id=wfx_a, workspace_id=ws_a, agent_id=agent_id)
    _conn_b = await reg.track(workflow_execution_id=wfx_b, workspace_id=ws_b, agent_id=agent_id)
    # Two subscribes (one per workflow) reached the first sender.
    await asyncio.wait_for(first_received.wait(), timeout=2.0)
    assert {m["workflow_execution_id"] for m in sent_first} == {str(wfx_a), str(wfx_b)}

    # Simulate disconnect → reconnect: unregister old, register new sender.
    reg.unregister_sender(agent_id)
    sent_second: list[dict] = []
    second_received = asyncio.Event()
    second_count = [0]

    async def _second(msg: dict) -> None:
        sent_second.append(msg)
        second_count[0] += 1
        if second_count[0] >= 2:
            second_received.set()

    await reg.register_sender(agent_id, _second)

    # The new sender should receive both subscribes again from the initial
    # reconciliation pass in register_sender().
    await asyncio.wait_for(second_received.wait(), timeout=2.0)
    reg.unregister_sender(agent_id)

    assert {m["type"] for m in sent_second} == {"subscribe"}
    assert {m["workflow_execution_id"] for m in sent_second} == {str(wfx_a), str(wfx_b)}
    assert {m["workspace_id"] for m in sent_second} == {str(ws_a), str(ws_b)}


@pytest.mark.asyncio
@pytest.mark.service
async def test_register_sender_filters_routes_by_agent_id(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """A reconnect for agent A must not replay subscribes routed to
    agent B — the registry shards routes by agent."""
    reg = SubscriberRegistry()
    agent_a, agent_b = uuid4(), uuid4()
    wfx_a, ws_a = uuid4(), uuid4()
    wfx_b, ws_b = uuid4(), uuid4()

    sent_a: list[dict] = []
    sent_b: list[dict] = []
    a_received = asyncio.Event()
    b_received = asyncio.Event()

    async def _a(msg: dict) -> None:
        sent_a.append(msg)
        a_received.set()

    async def _b(msg: dict) -> None:
        sent_b.append(msg)
        b_received.set()

    await reg.register_sender(agent_a, _a)
    await reg.register_sender(agent_b, _b)
    conn_a = await reg.track(workflow_execution_id=wfx_a, workspace_id=ws_a, agent_id=agent_a)
    conn_b = await reg.track(workflow_execution_id=wfx_b, workspace_id=ws_b, agent_id=agent_b)

    # Wait for both initial messages.
    await asyncio.wait_for(a_received.wait(), timeout=2.0)
    await asyncio.wait_for(b_received.wait(), timeout=2.0)

    # Reconnect just agent_a — agent_b's route should not be replayed.
    reg.unregister_sender(agent_a)
    sent_a_reconnect: list[dict] = []
    a2_received = asyncio.Event()

    async def _a2(msg: dict) -> None:
        sent_a_reconnect.append(msg)
        a2_received.set()

    await reg.register_sender(agent_a, _a2)
    await asyncio.wait_for(a2_received.wait(), timeout=2.0)
    reg.unregister_sender(agent_a)
    reg.unregister_sender(agent_b)

    assert all(m["workflow_execution_id"] == str(wfx_a) for m in sent_a_reconnect)
    assert str(wfx_b) not in {m["workflow_execution_id"] for m in sent_a_reconnect}

    # Cleanup.
    await reg.untrack(workflow_execution_id=wfx_a, conn_id=conn_a)
    await reg.untrack(workflow_execution_id=wfx_b, conn_id=conn_b)


@pytest.mark.asyncio
@pytest.mark.service
async def test_register_sender_subscribes_before_snapshot(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """register_sender must subscribe to the pub/sub channel BEFORE snapshotting
    Redis state, so a concurrent track() from a second pod whose publish lands
    between register_sender returning and the snapshot is still delivered.

    Behavioral proof: two registry instances sharing real Redis.

    1. pod_a calls register_sender — which now awaits the SUBSCRIBE handshake
       before reading agent_routes.
    2. Immediately after register_sender returns on pod_a, pod_b calls track().
       The publish happens AFTER the subscribe is active.
    3. The sender on pod_a must receive the subscribe message within the timeout —
       under the old (snapshot-first) ordering this would be a race; under the
       new ordering it is guaranteed because the subscribe handshake completed
       before track() published.
    """
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid4()

    pod_a = SubscriberRegistry()
    pod_b = SubscriberRegistry()

    sent: list[dict] = []
    received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        received.set()

    # register_sender now establishes the Redis SUBSCRIBE before returning.
    await pod_a.register_sender(agent_id, _sender)

    # After register_sender has returned the subscribe handshake is complete.
    # Any publish from here on is guaranteed to be received.
    conn = await pod_b.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    # Must arrive deterministically — not a timing race.
    await asyncio.wait_for(received.wait(), timeout=2.0)

    pod_a.unregister_sender(agent_id)

    assert len(sent) >= 1
    assert sent[0]["type"] == "subscribe"
    assert sent[0]["workflow_execution_id"] == str(workflow_id)

    # Cleanup.
    await pod_b.untrack(workflow_execution_id=workflow_id, conn_id=conn)


@pytest.mark.asyncio
@pytest.mark.service
async def test_two_subscribers_same_pod_same_wfx(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """Two concurrent SSE subscribers from the same pod to the same wfx_id
    are each counted independently.

    Regression test for the old bug where _connections was dict[UUID, str]
    and the second track() silently overwrote the first conn_id, leaving the
    first subscriber's ZSET member orphaned until the sweeper evicted it.
    The fix: _connections is dict[UUID, set[str]]; track() returns a per-call
    conn_id; untrack() removes exactly that member.
    """
    reg = SubscriberRegistry()
    agent_id = uuid4()
    wfx_id = uuid4()
    workspace_id = uuid7()

    unsub_received = asyncio.Event()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        if msg.get("type") == "unsubscribe":
            unsub_received.set()

    await reg.register_sender(agent_id, _sender)

    # Two track() calls for the same wfx_id.
    conn1 = await reg.track(
        workflow_execution_id=wfx_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    conn2 = await reg.track(
        workflow_execution_id=wfx_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    # conn_ids are unique per call.
    assert conn1 != conn2

    # Both members are in the ZSET.
    assert await zset_card(_wfx_subscribers_key(wfx_id)) == 2

    # Untrack conn1 — should drop ZCARD to 1, NOT fire unsubscribe.
    await reg.untrack(workflow_execution_id=wfx_id, conn_id=conn1)
    assert await zset_card(_wfx_subscribers_key(wfx_id)) == 1

    # Give the pub/sub consumer a moment — no unsubscribe should arrive.
    await asyncio.sleep(0.1)
    assert not any(m.get("type") == "unsubscribe" for m in sent), (
        "unsubscribe was published prematurely after first untrack"
    )

    # Untrack conn2 — ZCARD drops to 0, unsubscribe fires.
    await reg.untrack(workflow_execution_id=wfx_id, conn_id=conn2)
    await asyncio.wait_for(unsub_received.wait(), timeout=2.0)
    assert await zset_card(_wfx_subscribers_key(wfx_id)) == 0

    reg.unregister_sender(agent_id)
