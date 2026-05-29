"""SubscriberRegistry demand-pull semantics: 0→1 fires
subscribe, 1→0 fires unsubscribe, idempotent at edges."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    SubscriberRegistry,
    get_subscriber_registry,
)


@pytest.mark.asyncio
async def test_first_track_sends_subscribe() -> None:
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid4()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)
    await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    assert sent == [
        {
            "type": "subscribe",
            "workspace_id": str(workspace_id),
            "workflow_execution_id": str(workflow_id),
        }
    ]
    assert reg.count(workflow_id) == 1


@pytest.mark.asyncio
async def test_second_track_does_not_resend_subscribe() -> None:
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid4()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)
    await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    # Only the 0→1 transition dispatches a subscribe.
    assert len(sent) == 1
    assert reg.count(workflow_id) == 2


@pytest.mark.asyncio
async def test_last_untrack_sends_unsubscribe() -> None:
    reg = SubscriberRegistry()
    agent_id = uuid4()
    workflow_id = uuid4()
    workspace_id = uuid4()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)
    await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    await reg.untrack(workflow_execution_id=workflow_id)
    assert reg.count(workflow_id) == 1
    assert [m["type"] for m in sent] == ["subscribe"]

    await reg.untrack(workflow_execution_id=workflow_id)
    assert reg.count(workflow_id) == 0
    assert sent[-1] == {
        "type": "unsubscribe",
        "workspace_id": str(workspace_id),
        "workflow_execution_id": str(workflow_id),
    }


@pytest.mark.asyncio
async def test_untrack_below_zero_is_noop() -> None:
    reg = SubscriberRegistry()
    workflow_id = uuid4()
    await reg.untrack(workflow_execution_id=workflow_id)
    assert reg.count(workflow_id) == 0


@pytest.mark.asyncio
async def test_track_without_sender_doesnt_raise() -> None:
    """If the agent has disconnected (no sender registered) the registry
    still tracks the count — when the agent reconnects, the reconnect
    handler (follow-on) re-derives subscriptions."""
    reg = SubscriberRegistry()
    workflow_id = uuid4()
    workspace_id = uuid4()
    agent_id = uuid4()

    await reg.track(
        workflow_execution_id=workflow_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    # No exception, count went to 1.
    assert reg.count(workflow_id) == 1


@pytest.mark.asyncio
async def test_get_subscriber_registry_singleton() -> None:
    assert get_subscriber_registry() is get_subscriber_registry()


@pytest.mark.asyncio
async def test_register_sender_replays_subscribes_for_active_routes() -> None:
    """When the agent's WS reconnects, the new sender registration must
    re-emit subscribe messages for every active route pointing at that
    agent. Otherwise the agent's rebuilt SubscriptionSet stays empty and
    progress events drop until each UI detaches + re-attaches."""
    reg = SubscriberRegistry()
    agent_id = uuid4()
    wfx_a, ws_a = uuid4(), uuid4()
    wfx_b, ws_b = uuid4(), uuid4()

    # Initial connection: register sender, then track two workflows.
    sent_first: list[dict] = []

    async def _first(msg: dict) -> None:
        sent_first.append(msg)

    await reg.register_sender(agent_id, _first)
    await reg.track(workflow_execution_id=wfx_a, workspace_id=ws_a, agent_id=agent_id)
    await reg.track(workflow_execution_id=wfx_b, workspace_id=ws_b, agent_id=agent_id)
    # Two subscribes (one per workflow) reached the first sender.
    assert {m["workflow_execution_id"] for m in sent_first} == {str(wfx_a), str(wfx_b)}

    # Simulate disconnect → reconnect: unregister old, register new sender.
    await reg.unregister_sender(agent_id)
    sent_second: list[dict] = []

    async def _second(msg: dict) -> None:
        sent_second.append(msg)

    await reg.register_sender(agent_id, _second)

    # The new sender should receive both subscribes again, replayed in the
    # registry's stored order (dict iteration insertion order in Py 3.7+).
    assert len(sent_second) == 2
    assert {m["type"] for m in sent_second} == {"subscribe"}
    assert {m["workflow_execution_id"] for m in sent_second} == {str(wfx_a), str(wfx_b)}
    assert {m["workspace_id"] for m in sent_second} == {str(ws_a), str(ws_b)}


@pytest.mark.asyncio
async def test_register_sender_no_active_routes_sends_nothing() -> None:
    reg = SubscriberRegistry()
    agent_id = uuid4()
    sent: list[dict] = []

    async def _sender(msg: dict) -> None:
        sent.append(msg)

    await reg.register_sender(agent_id, _sender)
    assert sent == []


@pytest.mark.asyncio
async def test_register_sender_filters_routes_by_agent_id() -> None:
    """A reconnect for agent A must not replay subscribes routed to
    agent B — the registry shards routes by agent."""
    reg = SubscriberRegistry()
    agent_a, agent_b = uuid4(), uuid4()
    wfx_a, ws_a = uuid4(), uuid4()
    wfx_b, ws_b = uuid4(), uuid4()

    sent_a: list[dict] = []
    sent_b: list[dict] = []

    async def _a(msg: dict) -> None:
        sent_a.append(msg)

    async def _b(msg: dict) -> None:
        sent_b.append(msg)

    await reg.register_sender(agent_a, _a)
    await reg.register_sender(agent_b, _b)
    await reg.track(workflow_execution_id=wfx_a, workspace_id=ws_a, agent_id=agent_a)
    await reg.track(workflow_execution_id=wfx_b, workspace_id=ws_b, agent_id=agent_b)

    # Reconnect just agent_a — agent_b's route should not be replayed.
    await reg.unregister_sender(agent_a)
    sent_a_reconnect: list[dict] = []

    async def _a2(msg: dict) -> None:
        sent_a_reconnect.append(msg)

    await reg.register_sender(agent_a, _a2)
    assert [m["workflow_execution_id"] for m in sent_a_reconnect] == [str(wfx_a)]
