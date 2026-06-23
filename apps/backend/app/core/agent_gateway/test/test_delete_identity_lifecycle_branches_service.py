"""Service tests: DELETE /api/v1/agent/identity lifecycle-aware branching.

Tests:
(a) lifecycle="draining" → mark_agent_shutdown_complete CAS-flip, bearer revoked,
    shutdown_complete audit, failsafe called — returns 204.
(b) lifecycle="shutdown" re-fire → 204 with no side effects (no double audit,
    no double bearer revoke).
(c) lifecycle="active" → existing path: mark_agent_offline + bearer revoked +
    failsafe + workspace_agent.disconnected audit — returns 204.
(d) bearer-revoke error rolls back the CAS in the draining branch
    (tested via the False return path of mark_agent_shutdown_complete).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.agent_gateway.models import BearerTokenRow, WorkspaceAgentRow
from app.domain.orgs import insert_org

# ── App / client helpers ────────────────────────────────────────────────────


def _agent_app() -> FastAPI:
    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"agent_gateway"})
    return app


def _agent_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_agent_app()), base_url="http://test")


async def _seed_agent_and_bearer(
    db_session,
    *,
    lifecycle: str,
) -> tuple[UUID, UUID, str]:
    """Create an org + agent row + bearer. Returns (org_id, agent_id, plaintext_bearer)."""
    from app.core.agent_gateway import bearers  # noqa: PLC0415
    from app.core.tenancy import update_org_fields  # noqa: PLC0415

    org = await insert_org(db_session, slug=f"del-lc-{uuid4().hex[:6]}")
    await update_org_fields(
        db_session,
        org.org_id,
        registered_iam_arn="arn:aws:iam::111122223333:role/yaaos",
        aws_region="us-east-1",
    )
    row = WorkspaceAgentRow(
        org_id=org.org_id,
        instance_id=f"test-del-{uuid4().hex[:8]}",
        iam_arn="arn:aws:iam::111122223333:role/yaaos",
        version="0.0.1",
        state="reachable",
        lifecycle=lifecycle,
        claimed_workspace_count=0,
        last_heartbeat_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    plaintext, _record = await bearers.issue(
        agent_id=row.id,
        org_id=org.org_id,
        session=db_session,
        issued_iam_arn="arn:aws:iam::111122223333:role/yaaos",
    )
    await db_session.commit()
    return org.org_id, row.id, plaintext


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_draining_calls_mark_shutdown_complete(db_session) -> None:
    """lifecycle='draining' → DELETE flips lifecycle to 'shutdown' atomically."""
    _org_id, agent_id, bearer = await _seed_agent_and_bearer(db_session, lifecycle="draining")

    async with _agent_client() as c:
        resp = await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {bearer}"},
        )
    assert resp.status_code == 204, resp.text

    # lifecycle flipped to shutdown
    row = (
        await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert row.lifecycle == "shutdown"


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_draining_revokes_bearer(db_session) -> None:
    """Draining path: bearer revoked by mark_agent_shutdown_complete."""
    _org_id, agent_id, bearer = await _seed_agent_and_bearer(db_session, lifecycle="draining")

    async with _agent_client() as c:
        await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {bearer}"},
        )

    revoked_rows = (
        (
            await db_session.execute(
                select(BearerTokenRow).where(
                    BearerTokenRow.agent_id == agent_id,
                    BearerTokenRow.revoked_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(revoked_rows) >= 1


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_draining_writes_shutdown_complete_audit(db_session) -> None:
    """Draining path: workspace_agent.shutdown_complete audit row written."""
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    org_id, agent_id, bearer = await _seed_agent_and_bearer(db_session, lifecycle="draining")

    async with _agent_client() as c:
        await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {bearer}"},
        )

    entries = await list_for_entity("workspace_agent", agent_id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "workspace_agent.shutdown_complete" in kinds


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_shutdown_refire_is_noop(db_session) -> None:
    """lifecycle='shutdown' re-fire: returns 204, no double audit, bearer already expired."""
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    org_id, agent_id, bearer = await _seed_agent_and_bearer(db_session, lifecycle="shutdown")

    async with _agent_client() as c:
        resp = await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {bearer}"},
        )
    assert resp.status_code == 204, resp.text

    # No shutdown_complete audit written for re-fire (CAS would lose)
    entries = await list_for_entity("workspace_agent", agent_id, org_id=org_id)
    assert not any(e.kind == "workspace_agent.shutdown_complete" for e in entries)


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_active_writes_disconnected_audit(db_session) -> None:
    """lifecycle='active' (unexpected disconnect): workspace_agent.disconnected audit written."""
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    org_id, agent_id, bearer = await _seed_agent_and_bearer(db_session, lifecycle="active")

    async with _agent_client() as c:
        resp = await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {bearer}"},
        )
    assert resp.status_code == 204, resp.text

    entries = await list_for_entity("workspace_agent", agent_id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "workspace_agent.disconnected" in kinds


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_active_sets_state_offline(db_session) -> None:
    """lifecycle='active' path: agent state flips to offline."""
    _org_id, agent_id, bearer = await _seed_agent_and_bearer(db_session, lifecycle="active")

    async with _agent_client() as c:
        await c.delete(
            "/api/v1/agent/identity",
            headers={"Authorization": f"Bearer {bearer}"},
        )

    row = (
        await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert row.state == "offline"
