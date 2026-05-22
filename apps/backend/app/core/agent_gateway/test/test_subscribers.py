"""Phase 8b — SubscriberRegistry demand-pull semantics: 0→1 fires
subscribe, 1→0 fires unsubscribe, idempotent at edges."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    SubscriberRegistry,
    _reset_subscriber_registry_for_tests,
    get_subscriber_registry,
)


@pytest.fixture(autouse=True)
def _isolate_singleton() -> None:
    _reset_subscriber_registry_for_tests()
    yield
    _reset_subscriber_registry_for_tests()


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
