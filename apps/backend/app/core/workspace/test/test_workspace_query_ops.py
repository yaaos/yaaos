"""Service-layer ops for cross-module workspace queries.

Covers `get_workspace_claim_state`, `get_workspace_command_state`,
`update_workspace_status`, and `get_workspace_statuses` — the projection ops
that replace direct `WorkspaceRow` imports in consumers such as
`core/agent_gateway`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.workspace import (
    get_workspace_claim_state,
    get_workspace_command_state,
    get_workspace_statuses,
    update_workspace_status,
)
from app.core.workspace.models import WorkspaceRow


async def _seed_workspace(db_session, *, status: str = "active", **kwargs) -> WorkspaceRow:
    row = WorkspaceRow(
        id=uuid4(),
        org_id=uuid4(),
        provider_id="in_memory",
        provider="in_memory",
        spec={"sha": "abc123"},
        plugin_state={},
        status=status,
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
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
    cmd_id = uuid4()
    wfx_id = uuid4()
    ws = await _seed_workspace(
        db_session,
        status="active",
        current_command_id=cmd_id,
        current_holder_workflow_id=wfx_id,
    )

    state = await get_workspace_claim_state(cmd_id, db_session)

    assert state is not None
    assert state.workspace_id == ws.id
    assert state.current_holder_workflow_id == wfx_id
    assert state.status == "active"


@pytest.mark.asyncio
async def test_get_workspace_claim_state_no_holder_workflow_is_returned(db_session) -> None:
    """Returns a state even when `current_holder_workflow_id` is None —
    the guard logic in the caller detects that and raises StaleClaimError."""
    cmd_id = uuid4()
    ws = await _seed_workspace(
        db_session,
        status="active",
        current_command_id=cmd_id,
        current_holder_workflow_id=None,
    )

    state = await get_workspace_claim_state(cmd_id, db_session)

    assert state is not None
    assert state.workspace_id == ws.id
    assert state.current_holder_workflow_id is None


# ── get_workspace_command_state ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_workspace_command_state_returns_none_for_unknown_id(db_session) -> None:
    state = await get_workspace_command_state(uuid4(), db_session)
    assert state is None


@pytest.mark.asyncio
async def test_get_workspace_command_state_returns_projection(db_session) -> None:
    cmd_id = uuid4()
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
