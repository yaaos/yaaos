"""Service test: `dispatch_invocation` enqueues an AgentCommand + inserts a run row.

Drives the full call path (build_invocation → InvokeClaudeCodeCommand →
dispatch_via_workspace → enqueue_command + pin + try_claim → create_run)
against a real Postgres transaction. Assertions use only state owned by
`core/coding_agent` (the `coding_agent_runs` table via `CodingAgentRunRow`
and `get_run_id_for_command`) — the `agent_commands` row state belongs to
`core/agent_gateway` and is exercised in that module's own tests.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import select

import app.web  # noqa: F401 — registers all models so FK metadata resolves correctly
from app.core.coding_agent import (
    Invocation,
    dispatch_invocation,
)
from app.core.coding_agent.models import CodingAgentRunRow
from app.core.coding_agent.run_service import get_run_id_for_command
from app.core.workflow import CommandContext
from app.core.workspace import WorkspaceClaimFailed, WorkspaceNotFoundError
from app.testing.fake_coding_agent import FakeCodingAgentPlugin
from app.testing.seed import seed_agent, seed_workspace

pytestmark = pytest.mark.service


def _ctx(wfe_id: uuid.UUID, step_id: str = "review") -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(wfe_id),
        ticket_id=str(uuid.uuid4()),
        step_id=step_id,
        attempt=1,
        traceparent=None,
    )


def _invocation(workspace_id: UUID) -> Invocation:
    return Invocation(
        workspace_id=workspace_id,
        skill="pr_review",
        model="opus",
        effort="medium",
        context={"repo": "test-repo"},
        wallclock_seconds=300,
    )


async def _seed_active_workspace(org_id: UUID, session) -> UUID:  # type: ignore[no-untyped-def]
    """Insert a workspace row owned by a fresh agent; return its UUID."""
    agent = await seed_agent(org_id=org_id, session=session)
    ws_id_str = await seed_workspace(
        org_id=org_id,
        provider_id="remote_agent",
        sha="abc123",
        agent_id=agent["id"],
        caller_session=session,
    )
    return UUID(ws_id_str)


@pytest.mark.asyncio
async def test_dispatch_invocation_returns_uuid(db_session) -> None:
    """dispatch_invocation returns a UUID (the minted command_id)."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)

    command_id = await dispatch_invocation(
        invocation=_invocation(ws_id),
        plugin=FakeCodingAgentPlugin(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    assert isinstance(command_id, uuid.UUID)


@pytest.mark.asyncio
async def test_dispatch_invocation_inserts_run_row(db_session) -> None:
    """A `coding_agent_runs` row with status=running lands in the DB."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)

    command_id = await dispatch_invocation(
        invocation=_invocation(ws_id),
        plugin=FakeCodingAgentPlugin(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    row = (
        await db_session.execute(
            select(CodingAgentRunRow).where(CodingAgentRunRow.agent_command_id == command_id)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "running"
    assert row.workflow_execution_id == wfe_id
    assert row.plugin_id == "claude_code"


@pytest.mark.asyncio
async def test_dispatch_invocation_run_row_correlates_via_get_run_id_for_command(db_session) -> None:
    """get_run_id_for_command resolves the run by the returned command_id."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)

    command_id = await dispatch_invocation(
        invocation=_invocation(ws_id),
        plugin=FakeCodingAgentPlugin(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    run_id = await get_run_id_for_command(command_id, session=db_session)
    assert run_id is not None


@pytest.mark.asyncio
async def test_dispatch_invocation_run_row_step_id(db_session) -> None:
    """`step_id` on the run row matches the CommandContext's step_id."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)
    ctx = CommandContext(
        workflow_execution_id=str(wfe_id),
        ticket_id=str(uuid.uuid4()),
        step_id="code_review",
        attempt=1,
    )

    command_id = await dispatch_invocation(
        invocation=_invocation(ws_id),
        plugin=FakeCodingAgentPlugin(),
        ctx=ctx,
        session=db_session,
    )

    row = (
        await db_session.execute(
            select(CodingAgentRunRow).where(CodingAgentRunRow.agent_command_id == command_id)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.step_id == "code_review"


@pytest.mark.asyncio
async def test_dispatch_invocation_idempotent_command_id_is_uuidv7(db_session) -> None:
    """The returned command_id is a UUIDv7 (required by the FK check constraint on agent_commands)."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)

    command_id = await dispatch_invocation(
        invocation=_invocation(ws_id),
        plugin=FakeCodingAgentPlugin(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    # UUID version 7 encodes the timestamp in the most-significant bits and
    # sets the version nibble to 0x7.
    assert command_id.version == 7


@pytest.mark.asyncio
async def test_dispatch_invocation_different_calls_return_distinct_ids(db_session) -> None:
    """Each dispatch mints a fresh command_id."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    ws_id = await _seed_active_workspace(org_id, db_session)

    id1 = await dispatch_invocation(
        invocation=_invocation(ws_id),
        plugin=FakeCodingAgentPlugin(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    # Second dispatch must fail: workspace is now claimed from the first call.
    # Use a fresh workspace row so we can verify distinct IDs are minted.
    agent2 = await seed_agent(org_id=org_id, session=db_session)
    ws_id2 = UUID(
        await seed_workspace(
            org_id=org_id,
            provider_id="remote_agent",
            sha="def456",
            agent_id=agent2["id"],
            caller_session=db_session,
        )
    )
    wfe_id2 = uuid.uuid4()
    id2 = await dispatch_invocation(
        invocation=_invocation(ws_id2),
        plugin=FakeCodingAgentPlugin(),
        ctx=_ctx(wfe_id2),
        session=db_session,
    )

    assert id1 != id2


@pytest.mark.asyncio
async def test_dispatch_invocation_workspace_not_found_raises(db_session) -> None:
    """WorkspaceNotFoundError raised when workspace row does not exist."""
    nonexistent_ws_id = uuid.uuid4()
    with pytest.raises(WorkspaceNotFoundError):
        await dispatch_invocation(
            invocation=_invocation(nonexistent_ws_id),
            plugin=FakeCodingAgentPlugin(),
            ctx=_ctx(uuid.uuid4()),
            session=db_session,
        )


@pytest.mark.asyncio
async def test_dispatch_invocation_busy_workspace_raises_claim_failed(db_session) -> None:
    """WorkspaceClaimFailed raised when the workspace already has a current_command_id."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    # Seed a workspace that's already claimed (current_command_id pre-set).
    agent = await seed_agent(org_id=org_id, session=db_session)
    ws_id = UUID(
        await seed_workspace(
            org_id=org_id,
            provider_id="remote_agent",
            sha="abc",
            agent_id=agent["id"],
            current_command_id=uuid.uuid4(),  # pre-claimed
            caller_session=db_session,
        )
    )

    with pytest.raises(WorkspaceClaimFailed):
        await dispatch_invocation(
            invocation=_invocation(ws_id),
            plugin=FakeCodingAgentPlugin(),
            ctx=_ctx(wfe_id),
            session=db_session,
        )
