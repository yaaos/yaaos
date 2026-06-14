"""Service tests: heartbeat persists claimed_workspace_count from the payload.

`workspace_agents.claimed_workspace_count` is populated exclusively by the
heartbeat path — the identity exchange does not set it. The count is
`len(heartbeat.workspaces)`, not a wire field; the backend derives it here
as the single source of truth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.agent_gateway import bearers
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.domain.orgs import repository as orgs_repo

# ── Helpers ──────────────────────────────────────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _fixture_org_and_agent(db_session):
    org = await orgs_repo.insert_org(db_session, slug=f"hb-cnt-{uuid4().hex[:6]}")
    org.registered_iam_arn = f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}"
    org.aws_region = "us-east-1"
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org.org_id,
        instance_id=f"test-task-{uuid4().hex[:8]}",
        iam_arn=org.registered_iam_arn,
        version="0.0.1",
        state="reachable",
        claimed_workspace_count=0,
    )
    db_session.add(agent)
    await db_session.commit()

    plaintext, _ = await bearers.issue(
        agent_id=agent.id, org_id=org.org_id, session=db_session, source_ip="127.0.0.1"
    )
    await db_session.commit()
    return agent.id, plaintext


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_persists_claimed_workspace_count(db_session) -> None:
    """Heartbeat with N workspace entries sets claimed_workspace_count = N on the row."""
    agent_id, token = await _fixture_org_and_agent(db_session)

    ws_id_1 = uuid4()
    ws_id_2 = uuid4()
    ws_id_3 = uuid4()

    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "reported_at": datetime.now(UTC).isoformat(),
                "workspaces": [
                    {"workspace_id": str(ws_id_1), "status": "running"},
                    {"workspace_id": str(ws_id_2), "status": "running"},
                    {"workspace_id": str(ws_id_3), "status": "exited"},
                ],
            },
        )
    assert resp.status_code == 200, resp.text

    # Re-fetch the row from the DB to verify the count was persisted.
    row = (
        await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert row.claimed_workspace_count == 3


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_zero_workspaces_sets_count_to_zero(db_session) -> None:
    """Heartbeat with an empty workspaces list sets claimed_workspace_count = 0."""
    agent_id, token = await _fixture_org_and_agent(db_session)

    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "reported_at": datetime.now(UTC).isoformat(),
                "workspaces": [],
            },
        )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert row.claimed_workspace_count == 0


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_updates_count_on_subsequent_call(db_session) -> None:
    """claimed_workspace_count reflects the most recent heartbeat, not a cumulative."""
    agent_id, token = await _fixture_org_and_agent(db_session)

    ws_id = uuid4()

    async with _client() as c:
        # First heartbeat: 1 workspace
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "reported_at": datetime.now(UTC).isoformat(),
                "workspaces": [{"workspace_id": str(ws_id), "status": "running"}],
            },
        )
        assert resp.status_code == 200, resp.text

        # Second heartbeat: 0 workspaces
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "reported_at": datetime.now(UTC).isoformat(),
                "workspaces": [],
            },
        )
        assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert row.claimed_workspace_count == 0
