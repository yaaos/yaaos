"""Service test: shutdown_agents enqueues a ShutdownCommand; claim_next delivers it.

Flow: admin POSTs /agents/shutdown → DB lifecycle flips draining + ShutdownCommand
row inserted → agent (draining) calls claim_next → gets ShutdownCommand.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.agent_gateway.models import AgentCommandRow, WorkspaceAgentRow
from app.core.agent_gateway.service import claim_next, shutdown_agents
from app.core.agent_gateway.types import AgentCommandKind, ShutdownCommand
from app.core.audit_log import Actor, ActorKind
from app.testing.e2e_setup import seed_agent


async def _make_active_agent(db_session, *, org_id: UUID) -> UUID:
    """Insert an active workspace_agents row; return its id."""
    result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(result["id"]))
    # seed_agent inserts with lifecycle='unconfigured'; flip to active.
    from sqlalchemy import update  # noqa: PLC0415

    await db_session.execute(
        update(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id).values(lifecycle="active")
    )
    await db_session.flush()
    return agent_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_agents_enqueues_shutdown_command(db_session) -> None:
    """shutdown_agents inserts a ShutdownCommand row pre-stamped with the agent's id."""
    from sqlalchemy import select  # noqa: PLC0415

    org_id = uuid4()
    agent_id = await _make_active_agent(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    results = await shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_id],
        actor=actor,
        session=db_session,
    )
    assert results[0].outcome == "draining"
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.agent_id == agent_id,
                    AgentCommandRow.command_kind == AgentCommandKind.SHUTDOWN,
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
async def test_claim_next_delivers_shutdown_command_to_draining_agent(db_session) -> None:
    """A draining agent calling claim_next receives the ShutdownCommand."""
    org_id = uuid4()
    agent_id = await _make_active_agent(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    await shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_id],
        actor=actor,
        session=db_session,
    )
    await db_session.flush()

    cmd = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None
    assert isinstance(cmd, ShutdownCommand)
    assert cmd.kind == AgentCommandKind.SHUTDOWN
    assert cmd.completion_token is not None


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_does_not_deliver_shutdown_command_to_wrong_agent(db_session) -> None:
    """A ShutdownCommand for agent A is not delivered to agent B."""
    org_id = uuid4()
    agent_a = await _make_active_agent(db_session, org_id=org_id)
    agent_b = await _make_active_agent(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    await shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_a],
        actor=actor,
        session=db_session,
    )
    await db_session.flush()

    # agent_b is active with no commands pinned to it
    cmd = await claim_next(
        agent_b,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_agents_already_draining_no_command_enqueued(db_session) -> None:
    """Calling shutdown_agents on an already-draining agent returns 'already_draining'
    and does NOT enqueue a second ShutdownCommand."""
    from sqlalchemy import select  # noqa: PLC0415

    org_id = uuid4()
    agent_id = await _make_active_agent(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    # First call transitions to draining and enqueues one ShutdownCommand.
    await shutdown_agents(org_id=org_id, agent_ids=[agent_id], actor=actor, session=db_session)
    await db_session.flush()

    # Second call: agent is already draining — should short-circuit.
    results = await shutdown_agents(
        org_id=org_id,
        agent_ids=[agent_id],
        actor=actor,
        session=db_session,
    )
    assert results[0].outcome == "already_draining"
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.agent_id == agent_id,
                    AgentCommandRow.command_kind == AgentCommandKind.SHUTDOWN,
                )
            )
        )
        .scalars()
        .all()
    )
    # Only one ShutdownCommand from the first call.
    assert len(rows) == 1
