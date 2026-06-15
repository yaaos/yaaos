"""Service tests: per-endpoint authorization on the bearer HTTP endpoints.

Bearer identity is derived solely from the token — no path `agent_id`.
The `heartbeat` and `claim` endpoints operate on the bearer's own agent.
`post_workspace_event` / `post_command_event` enforce ownership via the
workspace's owning `agent_id` (within-org IDOR guard).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import httpx
import pytest
from fastapi import FastAPI

from app.core.agent_gateway import bearers
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.domain.orgs import repository as orgs_repo
from app.testing.seed import seed_workspace

# ── Helpers ──────────────────────────────────────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _insert_agent(db_session, org_id: UUID) -> UUID:
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org_id,
        instance_id=f"test-task-{uuid4().hex[:8]}",
        iam_arn=f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}",
        version="0.0.1",
        state="reachable",
    )
    db_session.add(agent)
    await db_session.commit()
    return agent.id


async def _two_agents_one_org(db_session) -> tuple[UUID, UUID, str]:
    """Insert one org with two agents; issue a bearer for agent A.

    Returns (agent_a_id, agent_b_id, bearer_for_a).
    """
    org = await orgs_repo.insert_org(db_session, slug=f"authz-{uuid4().hex[:6]}")
    org.registered_iam_arn = f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}"
    org.aws_region = "us-east-1"
    await db_session.commit()

    agent_a = await _insert_agent(db_session, org.org_id)
    agent_b = await _insert_agent(db_session, org.org_id)

    plaintext, _ = await bearers.issue(
        agent_id=agent_a, org_id=org.org_id, session=db_session, source_ip="127.0.0.1"
    )
    await db_session.commit()
    return agent_a, agent_b, plaintext


# ── Heartbeat: bearer-derived identity ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_uses_bearer_identity(db_session) -> None:
    """Heartbeat operates on the bearer's own agent — no path agent_id.
    A valid bearer produces 200 regardless of the agent it's issued to."""
    agent_a, _agent_b, token = await _two_agents_one_org(db_session)
    del agent_a
    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={"workspaces": [], "reported_at": "2026-01-01T00:00:00Z"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_rejects_missing_bearer(db_session) -> None:
    """Heartbeat with no Authorization header → 401."""
    _agent_a, _agent_b, _token = await _two_agents_one_org(db_session)
    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            json={"workspaces": [], "reported_at": "2026-01-01T00:00:00Z"},
        )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_rejects_empty_bearer(db_session) -> None:
    """Heartbeat with an empty bearer value → 401."""
    _agent_a, _agent_b, _token = await _two_agents_one_org(db_session)
    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": "Bearer "},
            json={"workspaces": [], "reported_at": "2026-01-01T00:00:00Z"},
        )
    assert resp.status_code == 401, resp.text


# ── Claim: bearer-derived identity ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_uses_bearer_identity(db_session) -> None:
    """Claim operates on the bearer's own agent (204 or 200 is fine)."""
    agent_a, _agent_b, token = await _two_agents_one_org(db_session)
    del agent_a
    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/commands/claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"wait_seconds": 0, "lifecycle": "unconfigured"},
        )
    assert resp.status_code in (200, 204), resp.text


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_rejects_missing_bearer(db_session) -> None:
    """Claim with no Authorization header → 401."""
    _agent_a, _agent_b, _token = await _two_agents_one_org(db_session)
    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/commands/claim",
            json={"wait_seconds": 0, "lifecycle": "unconfigured"},
        )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_rejects_empty_bearer(db_session) -> None:
    """Claim with an empty bearer value → 401."""
    _agent_a, _agent_b, _token = await _two_agents_one_org(db_session)
    async with _client() as c:
        resp = await c.post(
            "/api/v1/agent/commands/claim",
            headers={"Authorization": "Bearer "},
            json={"wait_seconds": 0, "lifecycle": "unconfigured"},
        )
    assert resp.status_code == 401, resp.text


# ── post_workspace_event / post_command_event ownership authz ──────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_workspace_event_rejects_foreign_owner(db_session) -> None:
    """A bearer for agent A posting a workspace_event for a workspace owned by
    agent B (same org) → 403."""
    agent_a, agent_b, token = await _two_agents_one_org(db_session)
    org_id = (await bearers.verify(token)).org_id  # type: ignore[union-attr]
    cmd_id = uuid7()
    ws_id = await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id,
        agent_id=agent_b,
        caller_session=db_session,
    )
    await db_session.commit()
    del agent_a
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/workspaces/{ws_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "workspace_id": ws_id,
                "command_id": str(cmd_id),
                "kind": "ready",
                "reported_at": datetime.now(UTC).isoformat(),
            },
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.service
async def test_workspace_event_allows_owner(db_session) -> None:
    """The owner posting its own workspace's event → 200."""
    agent_a, _agent_b, token = await _two_agents_one_org(db_session)
    org_id = (await bearers.verify(token)).org_id  # type: ignore[union-attr]
    cmd_id = uuid7()
    ws_id = await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id,
        agent_id=agent_a,
        caller_session=db_session,
    )
    await db_session.commit()
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/workspaces/{ws_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "workspace_id": ws_id,
                "command_id": str(cmd_id),
                "kind": "ready",
                "reported_at": datetime.now(UTC).isoformat(),
            },
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.service
async def test_command_event_rejects_foreign_owner(db_session) -> None:
    """A bearer for agent A posting a command_event for a command held by a
    workspace owned by agent B → 403."""
    agent_a, agent_b, token = await _two_agents_one_org(db_session)
    org_id = (await bearers.verify(token)).org_id  # type: ignore[union-attr]
    cmd_id = uuid7()
    await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id,
        agent_id=agent_b,
        caller_session=db_session,
    )
    await db_session.commit()
    del agent_a
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/commands/{cmd_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_id": str(cmd_id),
                "kind": "completed_success",
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.service
async def test_command_event_config_update_not_regressed(db_session) -> None:
    """An agent-scoped command (e.g. ConfigUpdate) resolves to no workspace,
    so there is no ownership edge to enforce — the per-agent authz check must
    NOT 403 it. It falls through to the stale-claim guard, which returns 410
    (matching the workspace-event handler). Proves no authz regression (403
    would mean authz incorrectly rejected the request)."""
    agent_a, _agent_b, token = await _two_agents_one_org(db_session)
    # No workspace holds this command_id — mirrors a ConfigUpdate terminal event
    # with a stale or unknown command_id.
    cmd_id = uuid7()
    del agent_a
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/commands/{cmd_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "command_id": str(cmd_id),
                "kind": "completed_success",
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    # 410 stale_claim, NOT 403 — authz let the agent-scoped command through.
    assert resp.status_code == 410, resp.text
    assert resp.json()["error"] == "stale_claim"


@pytest.mark.asyncio
@pytest.mark.service
async def test_workspace_event_rejects_missing_bearer(db_session) -> None:
    """workspace events endpoint with no bearer → 401."""
    ws_id = uuid4()
    cmd_id = uuid7()
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/workspaces/{ws_id}/events",
            json={
                "workspace_id": str(ws_id),
                "command_id": str(cmd_id),
                "kind": "ready",
                "reported_at": datetime.now(UTC).isoformat(),
            },
        )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.service
async def test_command_event_rejects_missing_bearer(db_session) -> None:
    """command events endpoint with no bearer → 401."""
    cmd_id = uuid7()
    async with _client() as c:
        resp = await c.post(
            f"/api/v1/commands/{cmd_id}/events",
            json={
                "command_id": str(cmd_id),
                "kind": "completed_success",
                "reported_at": datetime.now(UTC).isoformat(),
                "traceparent": "00-aabb-1122-01",
            },
        )
    assert resp.status_code == 401, resp.text
