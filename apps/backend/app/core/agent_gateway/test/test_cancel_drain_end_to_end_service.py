"""Service test: cancel_shutdown_agents enqueues a CancelShutdownCommand; claim_next delivers it.

Flow: admin POSTs /agents/cancel-shutdown → DB lifecycle flips active +
CancelShutdownCommand row inserted → agent (draining, still) calls claim_next
→ gets CancelShutdownCommand.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.agent_gateway.models import AgentCommandRow, WorkspaceAgentRow
from app.core.agent_gateway.service import cancel_shutdown_agents, claim_next, shutdown_agents
from app.core.agent_gateway.types import AgentCommandKind, CancelShutdownCommand
from app.core.audit_log import Actor, ActorKind
from app.testing.e2e_setup import seed_agent


async def _make_draining_agent(db_session, *, org_id: UUID) -> UUID:
    """Insert a workspace_agents row already in 'draining' lifecycle; return its id."""
    result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(result["id"]))
    from sqlalchemy import update  # noqa: PLC0415

    await db_session.execute(
        update(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id).values(lifecycle="draining")
    )
    await db_session.flush()
    return agent_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_agents_enqueues_cancel_command(db_session) -> None:
    """cancel_shutdown_agents inserts a CancelShutdownCommand row pre-stamped with agent_id."""
    from sqlalchemy import select  # noqa: PLC0415

    org_id = uuid4()
    agent_id = await _make_draining_agent(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    results = await cancel_shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_id],
        actor=actor,
        session=db_session,
    )
    assert results[0].outcome == "active"
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.agent_id == agent_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CANCEL_SHUTDOWN,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].agent_id == agent_id
    assert rows[0].completion_token_hash is not None


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_delivers_cancel_shutdown_command(db_session) -> None:
    """A draining agent calling claim_next receives the CancelShutdownCommand."""
    org_id = uuid4()
    agent_id = await _make_draining_agent(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    await cancel_shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_id],
        actor=actor,
        session=db_session,
    )
    await db_session.flush()

    # The agent is still reporting "draining" to the backend (localLifecycle
    # flips to "active" only after it executes CancelShutdownCommand locally).
    cmd = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None
    assert isinstance(cmd, CancelShutdownCommand)
    assert cmd.kind == AgentCommandKind.CANCEL_SHUTDOWN
    assert cmd.completion_token is not None


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_then_cancel_end_to_end(db_session) -> None:
    """Full round-trip: shutdown → claim ShutdownCommand → cancel → claim CancelShutdownCommand."""
    from sqlalchemy import update  # noqa: PLC0415

    org_id = uuid4()
    result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(result["id"]))
    await db_session.execute(
        update(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id).values(lifecycle="active")
    )
    await db_session.flush()

    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    # 1. Shutdown: lifecycle → draining, ShutdownCommand enqueued.
    sd_results = await shutdown_agents(org_id=org_id, agent_ids=[agent_id], actor=actor, session=db_session)
    assert sd_results[0].outcome == "draining"
    await db_session.flush()

    # 2. Agent (draining) claims → gets ShutdownCommand.
    shutdown_cmd = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert shutdown_cmd is not None
    assert isinstance(shutdown_cmd, CancelShutdownCommand) is False
    assert shutdown_cmd.kind == AgentCommandKind.SHUTDOWN

    # 3. Cancel: lifecycle → active, CancelShutdownCommand enqueued.
    cs_results = await cancel_shutdown_agents(
        org_id=org_id, agent_ids=[agent_id], actor=actor, session=db_session
    )
    assert cs_results[0].outcome == "active"
    await db_session.flush()

    # 4. Agent (still draining locally; next claim reports draining) → gets CancelShutdownCommand.
    cancel_cmd = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cancel_cmd is not None
    assert isinstance(cancel_cmd, CancelShutdownCommand)
    assert cancel_cmd.kind == AgentCommandKind.CANCEL_SHUTDOWN


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_not_draining_no_command_enqueued(db_session) -> None:
    """Calling cancel_shutdown_agents on an active agent returns 'not_draining'
    and does NOT enqueue a CancelShutdownCommand."""
    from sqlalchemy import select, update  # noqa: PLC0415

    org_id = uuid4()
    result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(result["id"]))
    await db_session.execute(
        update(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id).values(lifecycle="active")
    )
    await db_session.flush()

    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)
    results = await cancel_shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_id],
        actor=actor,
        session=db_session,
    )
    assert results[0].outcome == "not_draining"
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.agent_id == agent_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CANCEL_SHUTDOWN,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0
