"""Service tests: WS batch and HTTP republish paths deliver workspace-activity
events through `publish_workspace_activity` on the org-scoped channel.

All three sites call
`publish_workspace_activity(org_id=..., workflow_execution_id=..., payload=...)`
so the SPA subscribes to the namespaced `{org_id}:workspace_activity:{wfx_id}`
channel.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.core.agent_gateway import bearers
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.subscribers import _reset_subscriber_singleton_for_tests
from app.core.sse import reset_pubsub, subscribe_workspace_activity
from app.core.workspace import WorkspaceRow
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


@pytest.fixture(autouse=True)
def _isolate():
    _reset_subscriber_singleton_for_tests()
    reset_pubsub()
    bearers.set_verify_override(None)
    yield
    _reset_subscriber_singleton_for_tests()
    reset_pubsub()
    bearers.set_verify_override(None)


# ── Helpers ──────────────────────────────────────────────────────────────


async def _fixture_org_and_agent(db_session) -> tuple[UUID, UUID, str]:
    """Insert org + agent row + issue a bearer. Returns (org_id, agent_id, bearer_token)."""
    org = await orgs_repo.insert_org(db_session, slug=f"act-pub-{uuid4().hex[:6]}")
    org.registered_iam_arn = f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}"
    org.aws_region = "us-east-1"
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org.id,
        agent_pod_id=uuid4(),
        iam_arn=org.registered_iam_arn,
        version="0.0.1",
        state="reachable",
    )
    db_session.add(agent)
    await db_session.commit()

    plaintext, _ = await bearers.issue(
        agent_id=agent.id, org_id=org.id, session=db_session, source_ip="127.0.0.1"
    )
    await db_session.commit()

    return org.id, agent.id, plaintext


async def _seed_workspace_for_org(db_session, org_id: UUID) -> WorkspaceRow:
    """Seed a claimed workspace row for `org_id`. Returns the row + test-seeded ids."""
    cmd_id = uuid4()
    wfx_id = uuid4()
    row = WorkspaceRow(
        id=uuid4(),
        org_id=org_id,
        provider_id="in_memory",
        provider="remote_agent",
        spec={"sha": "deadbeef"},
        plugin_state={},
        status="active",
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
        current_command_id=cmd_id,
        current_holder_workflow_id=wfx_id,
    )
    db_session.add(row)
    await db_session.commit()
    row.__dict__["_test_seeded_command_id"] = cmd_id
    row.__dict__["_test_seeded_workflow_id"] = wfx_id
    return row


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_ws_batch_publishes_workspace_activity_with_org_id(db_session) -> None:
    """WS activity_batch handler calls `publish_workspace_activity` so events
    arrive on `subscribe_workspace_activity(org_id, wfx_id)`.

    Strategy: install a bearer stub that returns the known org_id; subscribe
    to the org-scoped channel; send an activity_batch over the WS; assert the
    event arrives on the subscription.
    """
    org_id, agent_id, token = await _fixture_org_and_agent(db_session)
    wfx_id = uuid4()

    # Install a bearer stub so the WS upgrade doesn't need a DB round-trip
    # inside the synchronous TestClient portal.
    async def _bearer_stub(tok: str) -> bearers.BearerContext | None:
        if tok != token:
            return None
        return bearers.BearerContext(bearer_id=uuid4(), agent_id=agent_id, org_id=org_id)

    bearers.set_verify_override(_bearer_stub)

    received: list[dict] = []

    async def _consume() -> None:
        async for evt in subscribe_workspace_activity(org_id, wfx_id):
            received.append(evt)
            if len(received) >= 2:
                return

    consumer = asyncio.create_task(_consume())
    # Allow the subscriber to register before the publisher fires.
    await asyncio.sleep(0.5)

    def _send_batch() -> None:
        with TestClient(_app()) as client:
            with client.websocket_connect(
                f"/api/v1/agents/{agent_id}/activity",
                headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                ws.send_json(
                    {
                        "type": "activity_batch",
                        "workflow_execution_id": str(wfx_id),
                        "events": [
                            {"kind": "agent.thought", "text": "thinking"},
                            {"kind": "agent.tool_use", "tool": "Read"},
                        ],
                    }
                )
                time.sleep(0.3)

    await asyncio.to_thread(_send_batch)
    await asyncio.wait_for(consumer, timeout=3.0)

    assert len(received) == 2
    assert received[0] == {"kind": "agent.thought", "text": "thinking"}
    assert received[1] == {"kind": "agent.tool_use", "tool": "Read"}
