"""Single-flight claim CAS — workspace dispatch tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.workspace import (
    release_claim,
    try_claim,
)
from app.core.workspace.models import WorkspaceRow
from app.testing.seed import seed_agent


async def _seed_active_workspace(db_session) -> WorkspaceRow:
    row = WorkspaceRow(
        id=uuid4(),
        org_id=uuid4(),
        provider_id="remote_agent",
        spec={"sha": "deadbeef"},
        plugin_state={},
        status="active",
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ── Single-flight claim ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_claim_succeeds_on_unclaimed_active_workspace(db_session) -> None:
    ws = await _seed_active_workspace(db_session)
    cmd_id = uuid4()
    wfx_id = uuid4()
    ok = await try_claim(ws.id, command_id=cmd_id, workflow_execution_id=wfx_id, session=db_session)
    assert ok is True

    await db_session.refresh(ws)
    assert ws.current_command_id == cmd_id
    assert ws.current_holder_workflow_id == wfx_id


@pytest.mark.asyncio
async def test_try_claim_persists_owning_agent_id(db_session) -> None:
    """When the caller passes `agent_id` (create-dispatch path), it's written
    onto the row alongside `current_command_id` so the workspace is hard-tied
    to its owning pod. owning_agent_id has a FK to workspace_agents.id."""
    seeded = await seed_agent(org_id=uuid4(), session=db_session)
    await db_session.flush()

    ws = await _seed_active_workspace(db_session)
    cmd_id = uuid4()
    wfx_id = uuid4()
    ok = await try_claim(
        ws.id,
        command_id=cmd_id,
        workflow_execution_id=wfx_id,
        agent_id=seeded["id"],
        session=db_session,
    )
    assert ok is True
    await db_session.refresh(ws)
    assert ws.owning_agent_id == seeded["id"]


@pytest.mark.asyncio
async def test_try_claim_without_agent_id_leaves_owner_null(db_session) -> None:
    """Callers that omit `agent_id` leave the row's owning_agent_id NULL."""
    ws = await _seed_active_workspace(db_session)
    ok = await try_claim(ws.id, command_id=uuid4(), workflow_execution_id=uuid4(), session=db_session)
    assert ok is True
    await db_session.refresh(ws)
    assert ws.owning_agent_id is None


@pytest.mark.asyncio
async def test_second_claim_loses_to_first(db_session) -> None:
    ws = await _seed_active_workspace(db_session)
    first_cmd, second_cmd = uuid4(), uuid4()
    first_wfx, second_wfx = uuid4(), uuid4()

    assert await try_claim(ws.id, command_id=first_cmd, workflow_execution_id=first_wfx, session=db_session)
    # Second claim attempt with a different command id while the first holds.
    assert not await try_claim(
        ws.id, command_id=second_cmd, workflow_execution_id=second_wfx, session=db_session
    )

    await db_session.refresh(ws)
    assert ws.current_command_id == first_cmd
    assert ws.current_holder_workflow_id == first_wfx


@pytest.mark.asyncio
async def test_try_claim_refuses_non_active_workspace(db_session) -> None:
    ws = await _seed_active_workspace(db_session)
    ws.status = "expired"
    await db_session.flush()

    ok = await try_claim(ws.id, command_id=uuid4(), workflow_execution_id=uuid4(), session=db_session)
    assert ok is False
    await db_session.refresh(ws)
    assert ws.current_command_id is None


@pytest.mark.asyncio
async def test_release_claim_clears_then_next_succeeds(db_session) -> None:
    ws = await _seed_active_workspace(db_session)
    cmd_id = uuid4()
    assert await try_claim(ws.id, command_id=cmd_id, workflow_execution_id=uuid4(), session=db_session)

    # Release returns True and clears current_command_id (holder_workflow stays
    # for reconciliation lookups).
    released = await release_claim(ws.id, command_id=cmd_id, session=db_session)
    assert released is True
    await db_session.refresh(ws)
    assert ws.current_command_id is None
    assert ws.current_holder_workflow_id is not None

    # A second release of the same command id is a no-op.
    assert (await release_claim(ws.id, command_id=cmd_id, session=db_session)) is False

    # Next try_claim succeeds.
    assert await try_claim(ws.id, command_id=uuid4(), workflow_execution_id=uuid4(), session=db_session)


@pytest.mark.asyncio
async def test_release_claim_with_wrong_command_id_is_noop(db_session) -> None:
    ws = await _seed_active_workspace(db_session)
    owner_cmd = uuid4()
    assert await try_claim(ws.id, command_id=owner_cmd, workflow_execution_id=uuid4(), session=db_session)

    # Release with a different command id leaves the claim intact.
    bogus = await release_claim(ws.id, command_id=uuid4(), session=db_session)
    assert bogus is False
    await db_session.refresh(ws)
    assert ws.current_command_id == owner_cmd
