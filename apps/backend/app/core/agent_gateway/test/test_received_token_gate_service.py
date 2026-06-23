"""Service tests: completion-token gate on RECEIVED AgentEvents.

Verifies that `record_agent_event` runs the row lookup and completion-token
check before branching on `event.kind`, so a RECEIVED event with a wrong or
missing token raises `StaleClaimError` and leaves the command row in `claimed`
status (lease not bumped).
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
