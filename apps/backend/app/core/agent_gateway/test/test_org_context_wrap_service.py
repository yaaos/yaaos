"""Service tests: bearer-auth endpoints enter org_context after auth resolves.

Two categories covered:
- Bearer HTTP endpoint (heartbeat) — verify `current_org_id()` equals the
  agent's org inside the handler.
- WebSocket endpoint (activity) — verify `current_org_id()` equals the
  agent's org for the connection lifetime.

The wrap is mechanical across all five bearer endpoints; the two tests
here guard against a future regression where a new endpoint is added
without the wrap.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.core.agent_gateway import bearers
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.auth import current_org_id
from app.domain.orgs import repository as orgs_repo

# ── Helpers ──────────────────────────────────────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _fixture_org_and_agent(db_session) -> tuple[UUID, UUID, str]:
    """Insert org + agent row + issue a bearer. Returns (org_id, agent_id, bearer_token_str)."""
    org = await orgs_repo.insert_org(db_session, slug=f"ctx-{uuid4().hex[:6]}")
    org.registered_iam_arn = f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}"
    org.aws_region = "us-east-1"
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org.org_id,
        agent_pod_id=uuid4(),
        iam_arn=org.registered_iam_arn,
        version="0.0.1",
        state="reachable",
    )
    db_session.add(agent)
    await db_session.commit()

    plaintext, _ = await bearers.issue(
        agent_id=agent.id, org_id=org.org_id, session=db_session, source_ip="127.0.0.1"
    )
    await db_session.commit()

    return org.org_id, agent.id, plaintext


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate():
    bearers.set_verify_override(None)
    yield
    bearers.set_verify_override(None)


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_endpoint_enters_org_context(db_session) -> None:
    """POST /agents/{id}/heartbeat — the handler body runs inside
    org_context(agent.org_id, ActorKind.WORKSPACE). `current_org_id()`
    must equal the agent's org_id during the request.

    Strategy: monkeypatch `record_heartbeat` to capture `current_org_id()`
    at call time, then restore the real function after the call.
    """
    org_id, agent_id, token = await _fixture_org_and_agent(db_session)

    captured: list[UUID | None] = []

    import app.core.agent_gateway.web as gw_web  # noqa: PLC0415

    real_record_heartbeat = gw_web.record_heartbeat

    async def _capturing_heartbeat(aid, req, *, session):
        captured.append(current_org_id())
        return await real_record_heartbeat(aid, req, session=session)

    gw_web.record_heartbeat = _capturing_heartbeat
    try:
        async with _client() as c:
            resp = await c.post(
                f"/api/v1/agents/{agent_id}/heartbeat",
                headers={"Authorization": f"Bearer {token}"},
                json={"workspaces": [], "reported_at": "2026-01-01T00:00:00Z"},
            )
        assert resp.status_code == 200, resp.text
    finally:
        gw_web.record_heartbeat = real_record_heartbeat

    assert len(captured) == 1, "record_heartbeat should have been called once"
    assert captured[0] == org_id, (
        f"current_org_id() inside the handler was {captured[0]!r}; expected {org_id!r}"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_activity_ws_endpoint_enters_org_context(db_session) -> None:
    """WSS /agents/{id}/activity — the connection lifetime runs inside
    org_context(agent.org_id, ActorKind.WORKSPACE). A message send
    (activity_batch or unknown) happens *within* the wrap, so
    `current_org_id()` is set at that point.

    Strategy: install a `bearers.verify` override that captures
    `current_org_id()` at verify time is too early (before the wrap).
    Instead, hook `core.sse.publish` — that's called from within the
    receive loop which is inside the wrap — and capture `current_org_id()`
    there.
    """
    org_id, agent_id, token = await _fixture_org_and_agent(db_session)

    # Install bearer verify override so the WS test doesn't hit DB timing
    # issues with the TestClient's synchronous event loop portal.
    async def _bearer_stub(tok: str) -> bearers.BearerContext | None:
        if tok != token:
            return None
        return bearers.BearerContext(bearer_id=uuid4(), agent_id=agent_id, org_id=org_id)

    bearers.set_verify_override(_bearer_stub)

    captured: list[UUID | None] = []

    import app.core.agent_gateway.web as gw_web  # noqa: PLC0415

    real_publish_workspace_activity = gw_web.publish_workspace_activity

    async def _capturing_publish_workspace_activity(*, org_id, workflow_execution_id, payload):
        captured.append(current_org_id())
        # Don't actually hit Redis in this test — just capture.

    gw_web.publish_workspace_activity = _capturing_publish_workspace_activity

    try:
        from starlette.testclient import TestClient  # noqa: PLC0415

        workflow_id = uuid4()
        app = _app()
        with TestClient(app) as client:
            with client.websocket_connect(
                f"/api/v1/agents/{agent_id}/activity",
                headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                ws.send_json(
                    {
                        "type": "activity_batch",
                        "workflow_execution_id": str(workflow_id),
                        "events": [{"kind": "progress", "message": "running"}],
                    }
                )
                # Give the server time to process the message.
                import time  # noqa: PLC0415

                time.sleep(0.1)
    finally:
        gw_web.publish_workspace_activity = real_publish_workspace_activity

    assert len(captured) >= 1, (
        "publish_workspace_activity should have been called at least once from within the WS handler"
    )
    assert captured[0] == org_id, (
        f"current_org_id() inside the WS handler was {captured[0]!r}; expected {org_id!r}"
    )
