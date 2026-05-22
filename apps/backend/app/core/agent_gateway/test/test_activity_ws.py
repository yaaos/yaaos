"""Phase 8b — Activity WebSocket: auth gate, sender registration,
activity_batch publishes to sse_pubsub, demand-pull semantics."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.agent_gateway import (
    _reset_subscriber_registry_for_tests,
    get_subscriber_registry,
)
from app.core.sse_pubsub import _reset_for_tests as _reset_pubsub
from app.core.sse_pubsub import channel_for, subscribe


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    spec = _specs["agent_gateway"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/v1")
    return app


@pytest.fixture(autouse=True)
def _isolate() -> None:
    _reset_subscriber_registry_for_tests()
    _reset_pubsub()
    yield
    _reset_subscriber_registry_for_tests()
    _reset_pubsub()


def test_ws_close_4401_when_missing_bearer() -> None:
    """The WebSocket upgrade requires an Authorization: Bearer <token>
    header. Empty / missing → close with 4401 before any messages flow."""
    app = _app()
    agent_id = uuid4()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/api/v1/agents/{agent_id}/activity"):
                pass
        assert exc_info.value.code == 4401


def test_ws_accepts_bearer_and_registers_sender() -> None:
    app = _app()
    agent_id = uuid4()
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/agents/{agent_id}/activity",
            headers={"Authorization": "Bearer test"},
        ):
            # While the WS is open, the registry has a sender for this agent.
            assert get_subscriber_registry().has_sender(agent_id)
        # After exit, the sender is unregistered.
        # Give the server-side task a chance to clean up.
        # (TestClient is synchronous so the disconnect handler ran by now.)
        assert not get_subscriber_registry().has_sender(agent_id)


@pytest.mark.asyncio
async def test_activity_batch_fans_out_to_sse_pubsub() -> None:
    """An incoming `activity_batch` carries `workflow_execution_id` (the
    agent learned it from the `subscribe` message it received) and the
    handler publishes each event to `activity:{workflow_execution_id}`
    on the in-memory pubsub."""
    app = _app()
    agent_id = uuid4()
    workflow_id = uuid4()
    channel = channel_for(str(workflow_id))

    received: list[dict] = []

    async def _consume() -> None:
        async for evt in subscribe(channel):
            received.append(evt)
            if len(received) == 2:
                return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.01)  # let consumer register

    # Run the WS client in a thread because TestClient.websocket_connect
    # is synchronous and we need to keep the event loop running for the
    # sse_pubsub consumer.
    def _send_batch() -> None:
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/api/v1/agents/{agent_id}/activity",
                headers={"Authorization": "Bearer test"},
            ) as ws:
                ws.send_json(
                    {
                        "type": "activity_batch",
                        "workflow_execution_id": str(workflow_id),
                        "events": [
                            {"kind": "agent.thought", "text": "hello"},
                            {"kind": "agent.tool_use", "tool": "Read"},
                        ],
                    }
                )

    await asyncio.to_thread(_send_batch)
    await asyncio.wait_for(consumer, timeout=2)

    assert received == [
        {"kind": "agent.thought", "text": "hello"},
        {"kind": "agent.tool_use", "tool": "Read"},
    ]


@pytest.mark.asyncio
async def test_publish_with_no_subscriber_drops_nothing_breaks_nothing() -> None:
    """Demand-pull: an `activity_batch` arriving when no SSE consumer is
    subscribed to the channel is a no-op (publish returns 0 deliveries).
    The architecture's "no events when nobody's watching" property is
    enforced at the supervisor side (it never emits a batch unless the
    backend has sent a `subscribe`). The backend doesn't try to filter
    again — if a batch arrives it publishes; with no listeners, nothing
    crashes."""
    app = _app()
    agent_id = uuid4()
    workflow_id = uuid4()

    def _send_batch() -> None:
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/api/v1/agents/{agent_id}/activity",
                headers={"Authorization": "Bearer test"},
            ) as ws:
                ws.send_json(
                    {
                        "type": "activity_batch",
                        "workflow_execution_id": str(workflow_id),
                        "events": [{"kind": "agent.thought"}],
                    }
                )

    await asyncio.to_thread(_send_batch)
    # No assertion — the test asserts by not raising.
