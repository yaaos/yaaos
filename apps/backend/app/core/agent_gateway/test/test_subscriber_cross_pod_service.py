"""Cross-pod subscribe/unsubscribe routing via Redis pub/sub.

Two `SubscriberRegistry` instances in one test process represent two backend
pods sharing the same Redis store. `track` on pod B triggers a subscribe
message delivered to the WS sender registered on pod A; `untrack` on pod B
triggers the corresponding unsubscribe. This directly satisfies C1's
requirements success-signal.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4, uuid7

import pytest

from app.core.agent_gateway.subscribers import SubscriberRegistry

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@pytest.mark.asyncio
@pytest.mark.service
async def test_cross_pod_track_delivers_subscribe_to_remote_sender(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """track() on pod_b delivers {type:subscribe} to the sender on pod_a.

    Proves the Redis pub/sub control channel correctly routes across pod
    boundaries: the only shared state is Redis; process-local `_senders`
    are private to each registry instance.
    """
    agent_id = uuid4()
    run_id = uuid4()
    workspace_id = uuid7()

    reg_pod_a = SubscriberRegistry()
    reg_pod_b = SubscriberRegistry()

    sent: list[dict] = []
    received = asyncio.Event()

    async def _fake_sender(msg: dict) -> None:
        sent.append(msg)
        received.set()

    # Pod A: register the WS sender (the agent's WebSocket terminates here).
    await reg_pod_a.register_sender(agent_id, _fake_sender)

    # Pod B: an SSE handler tracks a new UI subscriber for the same agent.
    conn = await reg_pod_b.track(
        run_id=run_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )

    # The pub/sub consumer on pod_a should deliver the subscribe envelope.
    await asyncio.wait_for(received.wait(), timeout=2.0)

    assert len(sent) == 1
    assert sent[0]["type"] == "subscribe"
    assert sent[0]["workspace_id"] == str(workspace_id)
    assert sent[0]["run_id"] == str(run_id)

    # --- second assertion: untrack → unsubscribe ---
    unsub_received = asyncio.Event()

    async def _fake_sender_2(msg: dict) -> None:
        sent.append(msg)
        if msg.get("type") == "unsubscribe":
            unsub_received.set()

    reg_pod_a.unregister_sender(agent_id)
    await reg_pod_a.register_sender(agent_id, _fake_sender_2)

    await reg_pod_b.untrack(run_id=run_id, conn_id=conn)
    await asyncio.wait_for(unsub_received.wait(), timeout=2.0)

    unsubs = [m for m in sent if m.get("type") == "unsubscribe"]
    assert len(unsubs) == 1
    assert unsubs[0]["workspace_id"] == str(workspace_id)
    assert unsubs[0]["run_id"] == str(run_id)

    # Cleanup.
    reg_pod_a.unregister_sender(agent_id)


@pytest.mark.asyncio
@pytest.mark.service
async def test_cross_pod_track_no_leakage_between_agents(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """track() for agent_b does not deliver to agent_a's sender.

    Pub/sub channels are keyed by agent_id; SADD writes to agent_b's SET.
    Sender for agent_a must receive nothing.
    """
    agent_a = uuid4()
    agent_b = uuid4()
    run_b = uuid4()
    ws_b = uuid7()

    reg = SubscriberRegistry()

    sent_a: list[dict] = []

    async def _sender_a(msg: dict) -> None:
        sent_a.append(msg)

    await reg.register_sender(agent_a, _sender_a)

    conn_b = await reg.track(
        run_id=run_b,
        workspace_id=ws_b,
        agent_id=agent_b,
    )

    # Give the pub/sub consumer a moment — it should NOT deliver to agent_a.
    await asyncio.sleep(0.2)
    reg.unregister_sender(agent_a)

    assert sent_a == [], f"agent_a's sender received unexpected messages: {sent_a}"

    # Cleanup.
    await reg.untrack(run_id=run_b, conn_id=conn_b)
