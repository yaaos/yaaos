"""Service-layer ops for cross-module workspace queries.

Covers `get_workspace_claim_state`, `get_workspace_command_state`,
`update_workspace_status`, and `get_workspace_statuses` — the projection ops
that replace direct `WorkspaceRow` imports in consumers such as
`core/agent_gateway`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import pytest

from app.core.workspace import (
    get_workspace_claim_state,
    get_workspace_command_state,
    get_workspace_statuses,
    update_workspace_status,
)
from app.core.workspace.models import WorkspaceRow


async def _seed_workspace(db_session, *, status: str = "active", **kwargs) -> WorkspaceRow:
    from app.testing.seed import seed_agent  # noqa: PLC0415

    agent = await seed_agent(org_id=uuid4(), session=db_session)
    row = WorkspaceRow(
        id=uuid7(),
        org_id=uuid4(),
        provider_id="remote_agent",
        spec={"sha": "abc123"},
        status=status,
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
        owning_agent_id=agent["id"],
        **kwargs,
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ── get_workspace_claim_state ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_claim_state_returns_none_when_no_match(db_session) -> None:
    state = await get_workspace_claim_state(uuid4(), db_session)
    assert state is None


@pytest.mark.asyncio
async def test_get_workspace_claim_state_returns_projection_for_claimed_workspace(db_session) -> None:
    """Projection contains workspace_id, status, and owning_agent_id."""
    cmd_id = uuid7()
    ws = await _seed_workspace(
        db_session,
        status="active",
        current_command_id=cmd_id,
    )

    state = await get_workspace_claim_state(cmd_id, db_session)

    assert state is not None
    assert state.workspace_id == ws.id
    assert state.status == "active"
    # current_holder_workflow_id column is gone.
    assert not hasattr(state, "current_holder_workflow_id")


@pytest.mark.asyncio
async def test_get_workspace_claim_state_returns_owning_agent_id(db_session) -> None:
    """owning_agent_id is included in the projection for the agent-authz check.

    The projection carries owning_agent_id (None when not set) so the caller
    can compare against the bearer's agent_id for the per-agent authz guard.
    """
    cmd_id = uuid7()
    ws = await _seed_workspace(
        db_session,
        status="active",
        current_command_id=cmd_id,
    )

    state = await get_workspace_claim_state(cmd_id, db_session)

    assert state is not None
    assert state.workspace_id == ws.id
    # owning_agent_id is in the projection.
    assert state.owning_agent_id is not None


# ── get_workspace_command_state ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_command_state_returns_none_for_unknown_id(db_session) -> None:
    state = await get_workspace_command_state(uuid4(), db_session)
    assert state is None


@pytest.mark.asyncio
async def test_get_workspace_command_state_returns_projection(db_session) -> None:
    cmd_id = uuid7()
    ws = await _seed_workspace(
        db_session,
        status="active",
        current_command_id=cmd_id,
    )

    state = await get_workspace_command_state(ws.id, db_session)

    assert state is not None
    assert state.workspace_id == ws.id
    assert state.current_command_id == cmd_id
    assert state.status == "active"


@pytest.mark.asyncio
async def test_get_workspace_command_state_null_command_id(db_session) -> None:
    ws = await _seed_workspace(db_session, status="active")

    state = await get_workspace_command_state(ws.id, db_session)

    assert state is not None
    assert state.current_command_id is None


# ── update_workspace_status ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_workspace_status_flips_status(db_session) -> None:
    ws = await _seed_workspace(db_session, status="active")

    await update_workspace_status(ws.id, "destroyed", db_session)
    await db_session.flush()
    await db_session.refresh(ws)

    assert ws.status == "destroyed"


# ── get_workspace_statuses ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_statuses_empty_input_returns_empty(db_session) -> None:
    result = await get_workspace_statuses(set(), db_session)
    assert result == {}


@pytest.mark.asyncio
async def test_get_workspace_statuses_returns_map_for_known_ids(db_session) -> None:
    ws_a = await _seed_workspace(db_session, status="active")
    ws_b = await _seed_workspace(db_session, status="destroyed")

    result = await get_workspace_statuses({ws_a.id, ws_b.id}, db_session)

    assert result[ws_a.id] == "active"
    assert result[ws_b.id] == "destroyed"


@pytest.mark.asyncio
async def test_get_workspace_statuses_omits_unknown_ids(db_session) -> None:
    ws = await _seed_workspace(db_session, status="active")
    ghost_id = uuid4()

    result = await get_workspace_statuses({ws.id, ghost_id}, db_session)

    assert ws.id in result
    assert ghost_id not in result
