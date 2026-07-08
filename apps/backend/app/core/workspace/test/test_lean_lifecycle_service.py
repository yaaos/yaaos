"""Service tests for the lean workspace lifecycle.

Covers:
- Workspace row created on the agent's first `created`/`ready` event with
  `owning_agent_id` from the reporting bearer and `org_id`/`spec` from the
  originating `agent_commands` row.
- The `ProvisionWorkspace`-success materialisation path: completion-token
  verification, TTL/provider-id derivation, and idempotent replay.
- `release_claim` runs before the next `try_claim` (failure-report-precedes-disposal).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from sqlalchemy import select

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    CleanupWorkspaceCommand,
    ProvisionWorkspaceCommand,
    RepoRef,
    StaleClaimError,
    WorkspaceEvent,
    enqueue_command,
    record_agent_event,
    record_workspace_event,
)
from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus
from app.testing.e2e_setup import seed_agent

pytestmark = pytest.mark.service


# ── Lean row creation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lean_row_created_on_first_workspace_event(db_session) -> None:
    """The `workspaces` row is created on the first `created`/`ready` event
    with `owning_agent_id` from the bearer and `org_id`/`spec` from the
    originating `agent_commands` row — no pre-created row needed."""
    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(agent_result["id"]))
    await db_session.flush()

    workspace_id = uuid7()
    command_id = uuid7()

    # Enqueue a ProvisionWorkspace command so there's a real agent_commands row.
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # No workspace row yet.
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    # Fire the first workspace event (kind=`created`) with the agent's id.
    event = WorkspaceEvent(
        workspace_id=workspace_id,
        command_id=command_id,
        kind="created",
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    # The lean row should now exist.
    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None, "lean workspace row should be created on first event"
    assert ws.status == WorkspaceStatus.ACTIVE.value
    assert ws.owning_agent_id == agent_id
    assert ws.org_id == org_id


@pytest.mark.asyncio
async def test_lean_row_not_created_for_unknown_kind(db_session) -> None:
    """Non-`created`/`ready` kinds when no row exists → sink returns accepted=False.
    `record_workspace_event` raises StaleClaimError in that case; no row is inserted."""
    org_id = uuid4()
    workspace_id = uuid7()
    command_id = uuid7()

    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    event = WorkspaceEvent(
        workspace_id=workspace_id,
        command_id=command_id,
        kind="destroyed",  # not in _ROW_CREATE_KINDS
        reported_at=datetime.now(UTC),
    )
    # Sending a terminal-status event to a non-existent workspace raises StaleClaimError.
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, agent_id=None, session=db_session)

    # No lean row was created.
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None


@pytest.mark.asyncio
async def test_lean_row_org_id_from_command_row(db_session) -> None:
    """The lean row's `org_id` must match the `agent_commands` row's `org_id`,
    not the agent's `org_id` (which matches here but the test checks the exact join)."""
    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(agent_result["id"]))
    await db_session.flush()

    workspace_id = uuid7()
    command_id = uuid7()
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    event = WorkspaceEvent(
        workspace_id=workspace_id,
        command_id=command_id,
        kind="ready",
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None
    assert ws.org_id == org_id


# ── ProvisionWorkspace success → lean row materialisation ────────────────


def _make_provision_command(
    *, workspace_id: UUID, command_id: UUID, ttl_seconds: int = 900
) -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="super-secret-installation-token"),
        ttl_seconds=ttl_seconds,
        max_idle_seconds=ttl_seconds,
    )


@pytest.mark.asyncio
async def test_provision_success_completion_token_verified(db_session) -> None:
    """A command claimed via `claim_next` mints a completion token. A terminal
    `completed_success` echoing the correct token materialises the lean row; a
    terminal event with a wrong/empty token raises StaleClaimError and creates
    no workspace row."""
    from app.core.agent_gateway import claim_next  # noqa: PLC0415

    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(agent_result["id"]))

    workspace_id = uuid7()
    command_id = uuid7()
    cmd = _make_provision_command(workspace_id=workspace_id, command_id=command_id)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Claim the command as the agent — this mints the completion token and
    # returns it on the command DTO.
    claimed = await claim_next(
        agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None
    assert claimed.command_id == command_id
    token = claimed.completion_token
    assert token, "claim_next must inject the raw completion token on the DTO"

    # A terminal event with a WRONG token is rejected and creates no row.
    bad_event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token="not-the-real-token",
    )
    with pytest.raises(StaleClaimError):
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(bad_event, agent_id=agent_id, session=db_session)
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    # An empty token is also rejected.
    empty_event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=None,
    )
    with pytest.raises(StaleClaimError):
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(empty_event, agent_id=agent_id, session=db_session)
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    # The correct token succeeds and materialises the lean row.
    good_event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=token,
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(good_event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None, "correct token should materialise the lean row"
    assert ws.status == WorkspaceStatus.ACTIVE.value
    assert ws.owning_agent_id == agent_id


@pytest.mark.asyncio
async def test_provision_success_materialises_lean_row(db_session) -> None:
    """The happy path materialises the lean row via the sink: status active,
    org/agent from the command + bearer, TTL from the payload, provider id from
    the registered provider, and a spec that carries the SHA but no token."""
    from datetime import timedelta  # noqa: PLC0415

    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(agent_result["id"]))

    workspace_id = uuid7()
    command_id = uuid7()
    ttl_seconds = 900
    cmd = _make_provision_command(workspace_id=workspace_id, command_id=command_id, ttl_seconds=ttl_seconds)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    before = datetime.now(UTC)
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None, "lean row should be materialised on ProvisionWorkspace success"
    assert ws.status == WorkspaceStatus.ACTIVE.value
    assert ws.org_id == org_id
    assert ws.owning_agent_id == agent_id
    assert ws.max_idle_seconds == ttl_seconds
    # expires_at derived from the payload TTL, not the default.
    assert ws.expires_at >= before + timedelta(seconds=ttl_seconds - 5)
    # provider id resolved via the registry (falls back to the single shipped
    # provider id when no provider is bound in this service-test context).
    assert ws.provider_id == "remote_agent"
    # spec carries the SHA only — never the installation token.
    assert ws.spec == {"sha": "deadbeef"}
    assert "auth" not in ws.spec
    assert "token" not in str(ws.spec)


@pytest.mark.asyncio
async def test_provision_success_idempotent(db_session) -> None:
    """Replaying the terminal `completed_success` does not insert a duplicate
    workspace row."""
    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(agent_result["id"]))

    workspace_id = uuid7()
    command_id = uuid7()
    cmd = _make_provision_command(workspace_id=workspace_id, command_id=command_id)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    # Replay the same terminal event. The command row is retired (status=done)
    # but still present, so the guard re-passes; the sink sees the existing
    # workspace row and skips the insert — no duplicate, no error.
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    rows = (await db_session.execute(select(WorkspaceRow.id).where(WorkspaceRow.id == workspace_id))).all()
    assert len(rows) == 1


# ── release_claim timing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_claim_before_next_try_claim(db_session) -> None:
    """After a terminal agent event, `current_command_id` is cleared before
    the run engine is resumed — so the next `try_claim` sees NULL."""
    from datetime import timedelta  # noqa: PLC0415

    from app.core.workspace.dispatch import try_claim  # noqa: PLC0415

    org_id = uuid4()
    workspace_id = uuid7()
    command_id = uuid7()

    # Seed a workspace row that holds the current command claim.
    agent_result = await seed_agent(org_id=org_id)
    ws = WorkspaceRow(
        id=workspace_id,
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "abc"},
        status=WorkspaceStatus.ACTIVE.value,
        current_command_id=command_id,
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
        owning_agent_id=agent_result["id"],
    )
    db_session.add(ws)
    await db_session.flush()

    # Enqueue an agent_commands row so record_agent_event finds it.
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Simulate a terminal agent event — this should call release_claim before
    # routing to the run engine.
    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)
    await db_session.flush()

    # After the terminal event, current_command_id must be None.
    await db_session.refresh(ws)
    assert ws.current_command_id is None, "release_claim must clear current_command_id before routing"

    # A subsequent try_claim should now succeed (claim is released).
    new_cmd_id = uuid7()
    claimed = await try_claim(
        workspace_id=workspace_id,
        command_id=new_cmd_id,
        run_id=uuid4(),
        session=db_session,
    )
    assert claimed, "try_claim should succeed after release_claim"
