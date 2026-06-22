"""SubscriberReconciler._reconcile_once() correctness.

Constructs a registry with a registered sender, pre-populates the Redis
ZSET and route HASH / agent-routes SET directly (simulating what `track`
would do on another pod), then calls `_reconcile_once()` and asserts the
sender receives the expected subscribe or unsubscribe envelope.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4, uuid7

import pytest

from app.core.agent_gateway import set_subscriber_registry_for_tests
from app.core.agent_gateway.subscribers import SubscriberReconciler
from app.core.redis import (
    hash_delete,
    hash_set,
    zset_add_member,
    zset_remove_member,
)

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@pytest.mark.asyncio
@pytest.mark.service
async def test_reconcile_once_sends_subscribe_when_zcard_nonzero(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """If ZSET has ≥1 member and the agent is not streaming, reconciler
    publishes a subscribe control message and the local sender receives it."""
    agent_id = uuid4()
    wfx_id = uuid4()
    workspace_id = uuid7()

    # Pre-populate Redis as if `track` ran on another pod.
    wfx_key = f"workflow_subscribers:{wfx_id}"
    route_key = f"workflow_route:{wfx_id}"
    routes_key = f"agent_routes:{agent_id}"
    member = f"other-pod:{uuid4()}"
    score = time.time()

    await zset_add_member(wfx_key, member, score)
    await hash_set(route_key, {"workspace_id": str(workspace_id), "agent_id": str(agent_id)})
    await zset_add_member(routes_key, str(wfx_id), time.time())

    sent: list[dict] = []
    received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        received.set()

    with set_subscriber_registry_for_tests() as reg:
        await reg.register_sender(agent_id, _sender)

        # Reconciler reads from _get() — must be the same instance as reg.
        reconciler = SubscriberReconciler()
        await reconciler._reconcile_once()

        # The reconciler should have published subscribe; the pub/sub consumer
        # on this registry instance should deliver it.
        await asyncio.wait_for(received.wait(), timeout=2.0)
        reg.unregister_sender(agent_id)

    assert any(m.get("type") == "subscribe" for m in sent), f"expected subscribe message; got {sent}"
    sub = next(m for m in sent if m.get("type") == "subscribe")
    assert sub["workflow_execution_id"] == str(wfx_id)
    assert sub["workspace_id"] == str(workspace_id)

    # Cleanup Redis state.
    await zset_remove_member(wfx_key, member)
    await hash_delete(route_key)
    await zset_remove_member(routes_key, str(wfx_id))


@pytest.mark.asyncio
@pytest.mark.service
async def test_reconcile_once_sends_unsubscribe_when_zcard_zero(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """If ZSET is empty and the agent IS streaming, reconciler publishes
    unsubscribe and the local sender receives it."""
    agent_id = uuid4()
    wfx_id = uuid4()
    workspace_id = uuid7()

    # Pre-populate agent_routes ZSET only — no workflow_subscribers ZSET members (ZCARD=0).
    routes_key = f"agent_routes:{agent_id}"
    route_key = f"workflow_route:{wfx_id}"
    await zset_add_member(routes_key, str(wfx_id), time.time())
    # Route HASH must be present for reconciler to resolve workspace_id.
    await hash_set(route_key, {"workspace_id": str(workspace_id), "agent_id": str(agent_id)})

    sent: list[dict] = []
    unsub_received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        if msg.get("type") == "unsubscribe":
            unsub_received.set()

    with set_subscriber_registry_for_tests() as reg:
        await reg.register_sender(agent_id, _sender)

        # Manually inject streaming state so the reconciler believes the agent
        # is streaming this wfx_id (simulates the case where the agent was told
        # to subscribe but the last SSE consumer has since disconnected).
        async with reg._lock:
            reg._streaming.setdefault(agent_id, set()).add(wfx_id)

        # Reconciler reads from _get() — must be the same instance as reg.
        reconciler = SubscriberReconciler()
        await reconciler._reconcile_once()

        await asyncio.wait_for(unsub_received.wait(), timeout=2.0)
        reg.unregister_sender(agent_id)

    unsubs = [m for m in sent if m.get("type") == "unsubscribe"]
    assert len(unsubs) >= 1
    assert unsubs[-1]["workflow_execution_id"] == str(wfx_id)

    # Cleanup.
    await zset_remove_member(routes_key, str(wfx_id))
    await hash_delete(route_key)
