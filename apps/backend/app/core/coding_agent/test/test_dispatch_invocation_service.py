"""Service test: `dispatch_invocation` enqueues an AgentCommand + inserts a run row.

Drives the full call path (enqueue_command → create_run → pin_command_to_agent)
against a real Postgres transaction. Assertions use only state owned by
`core/coding_agent` (the `coding_agent_runs` table via `CodingAgentRunRow` and
`get_run_id_for_command`) — the `agent_commands` row state belongs to
`core/agent_gateway` and is exercised in that module's own tests.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.web  # noqa: F401 — registers all models so FK metadata resolves correctly
from app.core.coding_agent import (
    InvokeCodingAgent,
    dispatch_invocation,
)
from app.core.coding_agent.models import CodingAgentRunRow
from app.core.coding_agent.run_service import get_run_id_for_command
from app.core.workflow import CommandContext
from app.testing.fake_coding_agent import FakeCodingAgentPlugin

pytestmark = pytest.mark.service


def _ctx(wfe_id: uuid.UUID) -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(wfe_id),
        ticket_id=str(uuid.uuid4()),
        step_id="review",
        attempt=1,
        traceparent=None,
    )


def _exec_block(wallclock: int = 300) -> InvokeCodingAgent:
    return InvokeCodingAgent(
        argv=["claude", "--print"],
        env={},
        stdin="stub prompt",
        wallclock_seconds=wallclock,
    )


@pytest.mark.asyncio
async def test_dispatch_invocation_returns_uuid(db_session) -> None:
    """dispatch_invocation returns a UUID (the minted command_id)."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()

    command_id = await dispatch_invocation(
        workspace_id=uuid.uuid4(),
        org_id=org_id,
        agent_id=uuid.uuid4(),
        workflow_execution_id=wfe_id,
        plugin=FakeCodingAgentPlugin(),
        invocation_data=_exec_block(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    assert isinstance(command_id, uuid.UUID)


@pytest.mark.asyncio
async def test_dispatch_invocation_inserts_run_row(db_session) -> None:
    """A `coding_agent_runs` row with status=running lands in the DB."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()

    command_id = await dispatch_invocation(
        workspace_id=uuid.uuid4(),
        org_id=org_id,
        agent_id=uuid.uuid4(),
        workflow_execution_id=wfe_id,
        plugin=FakeCodingAgentPlugin(),
        invocation_data=_exec_block(),
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
    wfe_id = uuid.uuid4()

    command_id = await dispatch_invocation(
        workspace_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow_execution_id=wfe_id,
        plugin=FakeCodingAgentPlugin(),
        invocation_data=_exec_block(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    run_id = await get_run_id_for_command(command_id, session=db_session)
    assert run_id is not None


@pytest.mark.asyncio
async def test_dispatch_invocation_run_row_step_id(db_session) -> None:
    """`step_id` on the run row matches the CommandContext's step_id."""
    wfe_id = uuid.uuid4()
    ctx = CommandContext(
        workflow_execution_id=str(wfe_id),
        ticket_id=str(uuid.uuid4()),
        step_id="code_review",
        attempt=1,
    )

    command_id = await dispatch_invocation(
        workspace_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow_execution_id=wfe_id,
        plugin=FakeCodingAgentPlugin(),
        invocation_data=_exec_block(),
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
    wfe_id = uuid.uuid4()

    command_id = await dispatch_invocation(
        workspace_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow_execution_id=wfe_id,
        plugin=FakeCodingAgentPlugin(),
        invocation_data=_exec_block(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    # UUID version 7 encodes the timestamp in the most-significant bits and
    # sets the version nibble to 0x7.
    assert command_id.version == 7


@pytest.mark.asyncio
async def test_dispatch_invocation_different_calls_return_distinct_ids(db_session) -> None:
    """Each dispatch mints a fresh command_id."""
    wfe_id = uuid.uuid4()
    kwargs = dict(
        workspace_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        workflow_execution_id=wfe_id,
        plugin=FakeCodingAgentPlugin(),
        invocation_data=_exec_block(),
        ctx=_ctx(wfe_id),
        session=db_session,
    )

    id1 = await dispatch_invocation(**kwargs)  # type: ignore[arg-type]
    # Mint a second invocation with a different workflow_execution_id so the
    # rows don't conflict on (wfe_id, step_id).
    wfe_id2 = uuid.uuid4()
    id2 = await dispatch_invocation(
        **{**kwargs, "workflow_execution_id": wfe_id2, "ctx": _ctx(wfe_id2)},  # type: ignore[arg-type]
    )

    assert id1 != id2
