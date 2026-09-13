"""SubscriberReconciler._reconcile_once() correctness.

Seeds shared memberships and route records without a control publication,
then reconciles and asserts that the sender receives the missing envelope.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4, uuid7

import pytest

from app.core.agent_gateway import set_subscriber_registry_for_tests
from app.core.agent_gateway.subscribers import RunRouteFields, SubscriberReconciler
from app.core.cache import delete_record, remove_member, touch_member, update_record

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@pytest.mark.asyncio
@pytest.mark.service
async def test_reconcile_once_sends_subscribe_when_members_present(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """If members exist and the agent is not streaming, reconciler
    publishes a subscribe control message and the local sender receives it."""
    agent_id = uuid4()
    run_id = uuid4()
    workspace_id = uuid7()

    # Seed as if another pod tracked without delivering its control message.
    run_key = f"run_subscribers:{run_id}"
    route_key = f"run_route:{run_id}"
    routes_key = f"agent_routes:{agent_id}"
    member = f"other-pod:{uuid4()}"
    sent: list[dict] = []
    received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        received.set()

    with set_subscriber_registry_for_tests() as reg:
        await reg.register_sender(agent_id, _sender)
        assert sent == []
        # Seed AFTER registration: reconnect replay cannot satisfy this test.
        await touch_member(run_key, member, last_seen=time.time())
        await update_record(route_key, fields=RunRouteFields(workspace_id=workspace_id, agent_id=agent_id))
        await touch_member(routes_key, str(run_id), last_seen=time.time())
        assert not reg.is_streaming(agent_id, run_id)

        # Reconciler reads from _get() — must be the same instance as reg.
        reconciler = SubscriberReconciler()
        await reconciler._reconcile_once()

        # The reconciler should have published subscribe; the pub/sub consumer
        # on this registry instance should deliver it.
        await asyncio.wait_for(received.wait(), timeout=2.0)
        reg.unregister_sender(agent_id)

    assert any(m.get("type") == "subscribe" for m in sent), f"expected subscribe message; got {sent}"
    sub = next(m for m in sent if m.get("type") == "subscribe")
    assert sub["run_id"] == str(run_id)
    assert sub["workspace_id"] == str(workspace_id)

    # Cleanup shared state.
    await remove_member(run_key, member)
    await delete_record(route_key)
    await remove_member(routes_key, str(run_id))


@pytest.mark.asyncio
@pytest.mark.service
async def test_reconcile_once_sends_unsubscribe_when_no_members(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """If membership is empty and the agent IS streaming, reconciler publishes
    unsubscribe and the local sender receives it."""
    agent_id = uuid4()
    run_id = uuid4()
    workspace_id = uuid7()

    # Seed a route with no subscriber membership.
    routes_key = f"agent_routes:{agent_id}"
    route_key = f"run_route:{run_id}"
    await touch_member(routes_key, str(run_id), last_seen=time.time())
    await update_record(route_key, fields=RunRouteFields(workspace_id=workspace_id, agent_id=agent_id))

    sent: list[dict] = []
    unsub_received = asyncio.Event()

    async def _sender(msg: dict) -> None:
        sent.append(msg)
        if msg.get("type") == "unsubscribe":
            unsub_received.set()

    with set_subscriber_registry_for_tests() as reg:
        await reg.register_sender(agent_id, _sender)

        # Manually inject streaming state so the reconciler believes the agent
        # is streaming this run_id (simulates the case where the agent was told
        # to subscribe but the last SSE consumer has since disconnected).
        async with reg._lock:
            reg._streaming.setdefault(agent_id, set()).add(run_id)

        # Reconciler reads from _get() — must be the same instance as reg.
        reconciler = SubscriberReconciler()
        await reconciler._reconcile_once()

        await asyncio.wait_for(unsub_received.wait(), timeout=2.0)
        reg.unregister_sender(agent_id)

    unsubs = [m for m in sent if m.get("type") == "unsubscribe"]
    assert len(unsubs) >= 1
    assert unsubs[-1]["run_id"] == str(run_id)

    # Cleanup.
    await remove_member(routes_key, str(run_id))
    await delete_record(route_key)
