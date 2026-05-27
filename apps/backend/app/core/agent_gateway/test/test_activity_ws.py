"""Phase 8b — Activity WebSocket: auth gate, sender registration,
activity_batch publishes to sse_pubsub, demand-pull semantics."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.agent_gateway import (
    bearers,
    get_subscriber_registry,
)
from app.core.agent_gateway.subscribers import _reset_for_tests as _reset_subscriber_registry
from app.core.sse_pubsub import channel_for, subscribe
from app.core.sse_pubsub.service import _reset_for_tests as _reset_pubsub

pytestmark = pytest.mark.usefixtures("redis_or_skip")


def _install_bearer_stub(agent_id: UUID) -> str:
    """Install a `bearers.verify` stub that accepts a fixed plaintext +
    returns a context bound to `agent_id`. Direct DB-backed bearer
    coverage lives in `test_bearers.py`; here we test the WS protocol
    without crossing event-loop boundaries between the test session and
    TestClient's portal."""
    expected = f"bearer-{uuid4().hex}"
    org_id = uuid4()

    async def _stub(token: str) -> bearers.BearerContext | None:
        if token != expected:
            return None
        return bearers.BearerContext(bearer_id=uuid4(), agent_id=agent_id, org_id=org_id)

    bearers.set_verify_override(_stub)
    return expected


def _app() -> FastAPI:

    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


@pytest.fixture(autouse=True)
def _isolate() -> None:
    _reset_subscriber_registry()
    _reset_pubsub()
    yield
    _reset_subscriber_registry()
    _reset_pubsub()
    bearers.set_verify_override(None)


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
    bearer = _install_bearer_stub(agent_id)
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/agents/{agent_id}/activity",
            headers={"Authorization": f"Bearer {bearer}"},
        ):
            # While the WS is open, the registry has a sender for this agent.
            assert get_subscriber_registry().has_sender(agent_id)
        # After exit, the sender is unregistered.
        # (TestClient is synchronous so the disconnect handler ran by now.)
        assert not get_subscriber_registry().has_sender(agent_id)


def test_ws_rejects_when_bearer_agent_id_does_not_match_path() -> None:
    """A bearer issued for pod A can't be used to upgrade the WS for pod B —
    stolen-bearer-cross-pod attack defence. Closes with 4403."""
    app = _app()
    agent_id_in_bearer = uuid4()
    different_path_agent = uuid4()
    bearer = _install_bearer_stub(agent_id_in_bearer)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/v1/agents/{different_path_agent}/activity",
                headers={"Authorization": f"Bearer {bearer}"},
            ):
                pass
        assert exc_info.value.code == 4403


@pytest.mark.asyncio
async def test_activity_batch_fans_out_to_sse_pubsub() -> None:
    """An incoming `activity_batch` carries `workflow_execution_id` (the
    agent learned it from the `subscribe` message it received) and the
    handler publishes each event to `activity:{workflow_execution_id}`
    on the in-memory pubsub."""
    app = _app()
    agent_id = uuid4()
    workflow_id = uuid4()
    bearer = _install_bearer_stub(agent_id)
    channel = channel_for(str(workflow_id))

    received: list[dict] = []

    async def _consume() -> None:
        async for evt in subscribe(channel):
            received.append(evt)
            if len(received) == 2:
                return

    consumer = asyncio.create_task(_consume())
    # Wait long enough for the Redis SUBSCRIBE round-trip to complete
    # before the publisher (in the thread below) starts sending. Local
    # Redis is sub-millisecond but the consumer task also has to be
    # scheduled.
    await asyncio.sleep(0.5)

    # Run the WS client in a thread because TestClient.websocket_connect
    # is synchronous and we need to keep the event loop running for the
    # sse_pubsub consumer. Without the small sleep before the with-exit,
    # the close races the server's receive_text() and the activity_batch
    # frame is dropped before the handler processes it (starlette's
    # TestClient WS portal: send_json is fire-and-forget; closing the WS
    # immediately afterwards can beat the server's frame consumption).
    def _send_batch() -> None:
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/api/v1/agents/{agent_id}/activity",
                headers={"Authorization": f"Bearer {bearer}"},
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
                # Give the server-side WS handler a chance to drain the
                # frame off the receive queue before we close.
                time.sleep(0.3)

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
    bearer = _install_bearer_stub(agent_id)

    def _send_batch() -> None:
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/api/v1/agents/{agent_id}/activity",
                headers={"Authorization": f"Bearer {bearer}"},
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
