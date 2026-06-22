"""Service test: `dispatch_via_workspace` enqueues + pins + optionally claims.

Drives the full Layer 2 call path against a real Postgres transaction.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import select

import app.web  # noqa: F401
from app.core.agent_gateway import CleanupWorkspaceCommand
from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceClaimFailed,
    WorkspaceNotFoundError,
    dispatch_via_workspace,
)
from app.core.workspace.models import WorkspaceRow
from app.testing.e2e_setup import seed_agent, seed_workspace

pytestmark = pytest.mark.service


def _cmd(ws_id: UUID) -> CleanupWorkspaceCommand:
    return CleanupWorkspaceCommand(
        command_id=uuid.uuid7(),
        workspace_id=ws_id,
        traceparent="",
    )


def _ctx(wfe_id: UUID | None = None) -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(wfe_id or uuid.uuid4()),
        ticket_id=str(uuid.uuid4()),
        step_id="cleanup",
        attempt=1,
    )


async def _seed_active_workspace(org_id: UUID, _session=None) -> UUID:  # type: ignore[no-untyped-def]
    agent = await seed_agent(org_id=org_id)
    ws_id_str = await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="abc",
        agent_id=agent["id"],
    )
    return UUID(ws_id_str)


@pytest.mark.asyncio
async def test_dispatch_via_workspace_returns_command_id(db_session) -> None:
    """dispatch_via_workspace returns the command's command_id."""
    org_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)
    cmd = _cmd(ws_id)

    returned = await dispatch_via_workspace(
        command=cmd,
        workspace_id=ws_id,
        ctx=_ctx(),
        session=db_session,
        claim_workspace=False,
    )

    assert returned == cmd.command_id


@pytest.mark.asyncio
async def test_dispatch_via_workspace_no_claim_leaves_workspace_unclaimed(db_session) -> None:
    """`claim_workspace=False` does NOT set current_command_id on the row."""
    org_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)
    cmd = _cmd(ws_id)

    await dispatch_via_workspace(
        command=cmd,
        workspace_id=ws_id,
        ctx=_ctx(),
        session=db_session,
        claim_workspace=False,
    )

    ws_row = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws_id))).scalar_one()
    assert ws_row.current_command_id is None


@pytest.mark.asyncio
async def test_dispatch_via_workspace_claim_sets_current_command_id(db_session) -> None:
    """`claim_workspace=True` atomically sets current_command_id on the row."""
    org_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)
    cmd = _cmd(ws_id)

    await dispatch_via_workspace(
        command=cmd,
        workspace_id=ws_id,
        ctx=_ctx(),
        session=db_session,
        claim_workspace=True,
    )

    ws_row = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws_id))).scalar_one()
    assert ws_row.current_command_id == cmd.command_id


@pytest.mark.asyncio
async def test_dispatch_via_workspace_not_found_raises(db_session) -> None:
    """WorkspaceNotFoundError raised when no workspace row exists."""
    ws_id = uuid.uuid4()
    cmd = _cmd(ws_id)

    with pytest.raises(WorkspaceNotFoundError):
        await dispatch_via_workspace(
            command=cmd,
            workspace_id=ws_id,
            ctx=_ctx(),
            session=db_session,
        )


@pytest.mark.asyncio
async def test_dispatch_via_workspace_claim_busy_raises(db_session) -> None:
    """WorkspaceClaimFailed raised when workspace already has a current_command_id."""
    org_id = uuid.uuid4()
    # Seed a pre-claimed workspace.
    agent = await seed_agent(org_id=org_id)
    ws_id = UUID(
        await seed_workspace(
            org_id=org_id,
            provider_id="remote_agent",
            sha="abc",
            agent_id=agent["id"],
            current_command_id=uuid.uuid4(),  # pre-claimed
        )
    )

    cmd = _cmd(ws_id)
    with pytest.raises(WorkspaceClaimFailed):
        await dispatch_via_workspace(
            command=cmd,
            workspace_id=ws_id,
            ctx=_ctx(),
            session=db_session,
            claim_workspace=True,
        )


@pytest.mark.asyncio
async def test_dispatch_via_workspace_claim_inactive_raises(db_session) -> None:
    """WorkspaceClaimFailed raised when workspace status is not 'active'."""
    org_id = uuid.uuid4()
    # Seed an expired workspace.
    agent = await seed_agent(org_id=org_id)
    ws_id = UUID(
        await seed_workspace(
            org_id=org_id,
            provider_id="remote_agent",
            sha="abc",
            agent_id=agent["id"],
            status="expired",
        )
    )

    cmd = _cmd(ws_id)
    with pytest.raises(WorkspaceClaimFailed):
        await dispatch_via_workspace(
            command=cmd,
            workspace_id=ws_id,
            ctx=_ctx(),
            session=db_session,
            claim_workspace=True,
        )
