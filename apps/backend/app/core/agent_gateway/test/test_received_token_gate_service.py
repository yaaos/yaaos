"""Service tests: completion-token gate on RECEIVED AgentEvents.

Verifies that `record_agent_event` runs the row lookup and completion-token
check before branching on `event.kind`, so a RECEIVED event with a wrong or
missing token raises `StaleClaimError` and leaves the command row in `claimed`
status (lease not bumped).

Also covers the Shutdown / CancelShutdown round-trip: those two agent-scoped
commands are pre-stamped with a placeholder hash at enqueue time (so they sit
outside the NULL-skip carve-out used for test-seeded rows), and `claim_next`
must overwrite the placeholder with the real freshly-minted hash so the agent's
RECEIVED event carrying the real token validates. The mechanism is the same as
ConfigUpdate; these tests are the explicit regression for the new code paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    ProvisionWorkspaceCommand,
    RepoRef,
    StaleClaimError,
    claim_next,
    enqueue_command,
    record_agent_event,
)
from app.testing.e2e_setup import seed_agent


async def _seed_and_claim(db_session) -> tuple[UUID, str]:
    """Seed a ProvisionWorkspace command, claim it to mint the token hash, and
    return ``(command_id, raw_token)``.

    Uses the transactional `db_session` throughout — no external commits, so
    the rollback fixture handles cleanup.
    """
    org_id = uuid4()
    agent = await seed_agent(org_id=org_id)
    agent_id: UUID = agent["id"]

    cmd_id = uuid7()
    command = ProvisionWorkspaceCommand(
        command_id=cmd_id,
        workspace_id=uuid4(),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="redacted"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )
    await enqueue_command(org_id=org_id, command=command, session=db_session)
    await db_session.flush()

    # Claim the command to mint the completion_token_hash on the row.
    claimed = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None, "claim_next returned None — enqueue or flush may have failed"
    assert claimed.command_id == cmd_id

    raw_token = claimed.completion_token
    assert raw_token is not None, "claim_next must mint a completion_token"

    return cmd_id, raw_token


@pytest.mark.asyncio
@pytest.mark.service
async def test_received_event_wrong_token_raises_stale_claim(db_session) -> None:
    """RECEIVED with a wrong completion token raises StaleClaimError and does not
    flip the command row from `claimed` to `delivered`."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    cmd_id, _correct_token = await _seed_and_claim(db_session)

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.RECEIVED,
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
        completion_token="this-is-not-the-right-token",
    )

    with pytest.raises(StaleClaimError):
        await record_agent_event(event, session=db_session)

    # Row must stay in `claimed` — the lease was NOT bumped.
    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == cmd_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "claimed", f"expected status='claimed' (lease not bumped), got {row.status!r}"


@pytest.mark.asyncio
@pytest.mark.service
async def test_received_event_correct_token_bumps_lease(db_session) -> None:
    """RECEIVED with the correct completion token succeeds and flips the command
    row from `claimed` to `delivered`, cancelling the reaper's requeue window."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    cmd_id, correct_token = await _seed_and_claim(db_session)

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.RECEIVED,
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
        completion_token=correct_token,
    )

    # Must not raise.
    await record_agent_event(event, session=db_session)
    await db_session.flush()

    # Row must have transitioned from `claimed` to `delivered`.
    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == cmd_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "delivered", f"expected status='delivered' (lease bumped), got {row.status!r}"


# ── Shutdown / CancelShutdown round-trip ────────────────────────────────────


async def _seed_active_agent_id(db_session, *, org_id: UUID) -> UUID:
    """Seed an agent in `org_id` and flip its lifecycle to active (shutdown_agents
    only acts on `unconfigured | active`)."""
    from sqlalchemy import update  # noqa: PLC0415

    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    seeded = await seed_agent(org_id=org_id)
    agent_id = UUID(str(seeded["id"]))
    await db_session.execute(
        update(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id).values(lifecycle="active")
    )
    await db_session.flush()
    return agent_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_shutdown_command_token_round_trip(db_session) -> None:
    """Full ShutdownCommand token round-trip exercises the gate end-to-end.

    1. `shutdown_agents` enqueues a ShutdownCommand row pre-stamped with a
       placeholder `completion_token_hash` (so the row sits outside the
       NULL-skip test-seed carve-out — the gate IS enforced).
    2. `claim_next` overwrites the placeholder with `sha256(new_token)` and
       returns the real token to the agent.
    3. `record_agent_event` with the real token bumps `claimed → delivered`.
    4. `record_agent_event` with an arbitrary other token raises
       `StaleClaimError` (gate rejects non-real tokens).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415
    from app.core.agent_gateway.service import claim_next, shutdown_agents  # noqa: PLC0415
    from app.core.agent_gateway.types import AgentCommandKind  # noqa: PLC0415
    from app.core.audit_log import Actor, ActorKind  # noqa: PLC0415

    org_id = uuid4()
    agent_id = await _seed_active_agent_id(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    # 1. Enqueue. Row is pre-stamped with a placeholder hash.
    await shutdown_agents(org_id=org_id, agent_ids=[agent_id], actor=actor, session=db_session)
    await db_session.flush()

    placeholder_row = (
        await db_session.execute(
            select(AgentCommandRow).where(
                AgentCommandRow.agent_id == agent_id,
                AgentCommandRow.command_kind == AgentCommandKind.SHUTDOWN,
            )
        )
    ).scalar_one()
    placeholder_hash = placeholder_row.completion_token_hash
    assert placeholder_hash is not None, "shutdown_agents must pre-stamp completion_token_hash"
    cmd_id = placeholder_row.id

    # 2. Claim. claim_next overwrites the placeholder hash and returns the real token.
    claimed = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None and claimed.command_id == cmd_id
    real_token = claimed.completion_token
    assert real_token is not None

    await db_session.refresh(placeholder_row)
    assert placeholder_row.completion_token_hash != placeholder_hash, (
        "claim_next must overwrite the placeholder hash with sha256(new_token)"
    )

    # 3. record_agent_event with the real token → accepted.
    received_real = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.RECEIVED,
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=real_token,
    )
    await record_agent_event(received_real, session=db_session)
    await db_session.flush()

    await db_session.refresh(placeholder_row)
    assert placeholder_row.status == "delivered", (
        f"real-token RECEIVED must flip claimed→delivered, got {placeholder_row.status!r}"
    )

    # 4. record_agent_event with an arbitrary other token → rejected.
    # The agent never sees the raw placeholder string (only its sha256), so the
    # most faithful "rejected by the gate" assertion is any non-real token.
    received_bogus = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.RECEIVED,
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token="not-the-real-token",
    )
    with pytest.raises(StaleClaimError):
        await record_agent_event(received_bogus, session=db_session)


@pytest.mark.asyncio
@pytest.mark.service
async def test_cancel_shutdown_command_token_round_trip(db_session) -> None:
    """Parallel round-trip for CancelShutdownCommand — same placeholder-then-overwrite
    pattern; this guards the cancel-drain path against any future regression in
    `cancel_shutdown_agents`'s pre-stamp or `claim_next`'s unconditional overwrite."""
    from sqlalchemy import select, update  # noqa: PLC0415

    from app.core.agent_gateway.models import AgentCommandRow, WorkspaceAgentRow  # noqa: PLC0415
    from app.core.agent_gateway.service import (  # noqa: PLC0415
        cancel_shutdown_agents,
        claim_next,
        shutdown_agents,
    )
    from app.core.agent_gateway.types import AgentCommandKind  # noqa: PLC0415
    from app.core.audit_log import Actor, ActorKind  # noqa: PLC0415

    org_id = uuid4()
    agent_id = await _seed_active_agent_id(db_session, org_id=org_id)
    actor = Actor(kind=ActorKind.USER, user_id=uuid4(), org_id=org_id)

    # Drain first so cancel_shutdown_agents has something to cancel. Mark the
    # ShutdownCommand 'done' so it doesn't crowd out the CancelShutdownCommand in
    # claim_next's FIFO bucket.
    await shutdown_agents(org_id=org_id, agent_ids=[agent_id], actor=actor, session=db_session)
    await db_session.flush()
    await db_session.execute(
        update(AgentCommandRow)
        .where(
            AgentCommandRow.agent_id == agent_id,
            AgentCommandRow.command_kind == AgentCommandKind.SHUTDOWN,
        )
        .values(status="done")
    )

    # Cancel the drain. Pre-stamps a placeholder hash on the CancelShutdownCommand row.
    cancel_result = await cancel_shutdown_agents(
        org_id=org_id, agent_ids=[agent_id], actor=actor, session=db_session
    )
    assert cancel_result[0].outcome == "active"
    await db_session.flush()

    # Lifecycle flipped back to active — claim_next will use the active gate.
    lifecycle_row = (
        await db_session.execute(select(WorkspaceAgentRow.lifecycle).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert lifecycle_row == "active"

    placeholder_row = (
        await db_session.execute(
            select(AgentCommandRow).where(
                AgentCommandRow.agent_id == agent_id,
                AgentCommandRow.command_kind == AgentCommandKind.CANCEL_SHUTDOWN,
            )
        )
    ).scalar_one()
    placeholder_hash = placeholder_row.completion_token_hash
    assert placeholder_hash is not None, "cancel_shutdown_agents must pre-stamp completion_token_hash"
    cmd_id = placeholder_row.id

    claimed = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None and claimed.command_id == cmd_id
    real_token = claimed.completion_token
    assert real_token is not None

    await db_session.refresh(placeholder_row)
    assert placeholder_row.completion_token_hash != placeholder_hash, (
        "claim_next must overwrite the placeholder hash on CancelShutdownCommand too"
    )

    received_real = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.RECEIVED,
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=real_token,
    )
    await record_agent_event(received_real, session=db_session)
    await db_session.flush()

    await db_session.refresh(placeholder_row)
    assert placeholder_row.status == "delivered", (
        f"real-token RECEIVED on CancelShutdownCommand must flip claimed→delivered, "
        f"got {placeholder_row.status!r}"
    )

    received_bogus = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.RECEIVED,
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token="not-the-real-token",
    )
    with pytest.raises(StaleClaimError):
        await record_agent_event(received_bogus, session=db_session)
