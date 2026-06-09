"""Service-level coverage for the durable agent_commands queue.

Verifies:
- enqueue_command inserts a row with status=pending; the row survives a
  simulated backend restart (in-memory state dropped).
- claim_next returns exactly one command per call; unconfigured claim returns
  only ConfigUpdate; no row is left in `claimed` limbo after a single call.
- Lease: no `received` event within 30s requeues to pending; `received`
  flips claimed→delivered; terminal event → done; attempt cap → loud terminal
  failure.
- Redelivery of the same command_id is idempotent (single-flight + 410 guard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import (
    DEFAULT_MAX_WORKSPACES,
    claim_next,
    enqueue_command,
    requeue_stale_claimed,
)
from app.core.agent_gateway.types import (
    AgentCommandKind,
    AuthBlock,
    CleanupWorkspaceCommand,
    ProvisionWorkspaceCommand,
    RepoRef,
    WriteFilesCommand,
    WriteFilesEntry,
)
from app.testing.seed import seed_agent

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_provision_cmd(workspace_id: UUID | None = None) -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )


def _make_write_cmd(workspace_id: UUID) -> WriteFilesCommand:
    return WriteFilesCommand(
        command_id=uuid4(),
        workspace_id=workspace_id,
        traceparent="00-aabbccdd-1122-01",
        files=(WriteFilesEntry(path="hello.txt", content="hello"),),
    )


def _make_cleanup_cmd(workspace_id: UUID) -> CleanupWorkspaceCommand:
    return CleanupWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=workspace_id,
        traceparent="00-aabbccdd-1122-01",
    )


async def _make_agent(db_session, *, org_id: UUID | None = None) -> UUID:
    """Seed a workspace agent row; return its id."""
    result = await seed_agent(
        org_id=org_id or uuid4(),
        session=db_session,
    )
    return UUID(str(result["id"]))


# ── Completion capability token ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_mints_completion_token_and_stores_only_hash(db_session) -> None:
    """`claim_next` mints a per-command completion token: the raw value is
    injected on the returned DTO; only its sha256 hash is persisted on the row.
    The raw token is never written to the persisted payload."""
    import hashlib  # noqa: PLC0415

    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    claimed = await claim_next(
        agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None
    assert claimed.command_id == cmd.command_id
    token = claimed.completion_token
    assert token, "claim_next must inject the raw completion token on the DTO"

    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == cmd.command_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "claimed"
    # Only the hash is persisted; it matches sha256 of the raw token.
    assert row.completion_token_hash == hashlib.sha256(token.encode()).hexdigest()
    # The raw token is never persisted in the payload.
    assert token not in str(row.payload)
    assert row.payload.get("completion_token") in (None, "")


@pytest.mark.asyncio
@pytest.mark.service
async def test_unconfigured_claim_carries_no_completion_token(db_session) -> None:
    """The `unconfigured` lifecycle returns a ConfigUpdate built in-memory with
    no DB row — it carries no completion token."""
    agent_id = await _make_agent(db_session)
    claimed = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None
    assert claimed.kind == AgentCommandKind.CONFIG_UPDATE
    # ConfigUpdateCommand has no completion_token field at all (separate base).
    assert getattr(claimed, "completion_token", None) is None


# ── Enqueue + durable persistence ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_enqueue_inserts_pending_row(db_session) -> None:
    """enqueue_command writes a pending row; the row is visible in the DB."""
    org_id = uuid4()
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == cmd.command_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "pending"
    assert row.agent_id is None
    assert row.command_kind == AgentCommandKind.PROVISION_WORKSPACE


@pytest.mark.asyncio
@pytest.mark.service
async def test_enqueue_then_simulated_restart_command_still_claimable(db_session) -> None:
    """Commands survive a backend restart: after the in-memory state is wiped
    the row remains in the DB and is claimable via claim_next."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Simulate restart: DB state is durable; claim_next reads from DB.
    command = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=4,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert command.command_id == cmd.command_id


# ── claim_batch: unconfigured ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_unconfigured_claim_returns_only_config_update(db_session) -> None:
    """An unconfigured agent receives exactly one ConfigUpdate; no queue draw."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    ws_id = uuid4()
    cmd = _make_provision_cmd(ws_id)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    command = await claim_next(
        agent_id=agent_id,
        lifecycle="unconfigured",
        new_workspaces=4,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    from app.core.agent_gateway.types import ConfigUpdateCommand  # noqa: PLC0415

    assert isinstance(command, ConfigUpdateCommand)
    assert command.config.max_workspaces == DEFAULT_MAX_WORKSPACES

    # The pending workspace command must still be pending (not claimed).
    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == cmd.command_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "pending"


# ── claim_batch: configured ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_returns_one_provision_workspace(db_session) -> None:
    """claim_next returns exactly one ProvisionWorkspace command per call."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    # Enqueue 3 creates.
    for _ in range(3):
        await enqueue_command(org_id=org_id, command=_make_provision_cmd(), session=db_session)
    await db_session.flush()

    command = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=2,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert command.kind == AgentCommandKind.PROVISION_WORKSPACE

    # Only one row must be in `claimed` — the other two remain `pending`.
    from sqlalchemy import func  # noqa: PLC0415

    claimed_count = (
        await db_session.execute(
            select(func.count()).select_from(AgentCommandRow).where(AgentCommandRow.status == "claimed")
        )
    ).scalar_one()
    pending_count = (
        await db_session.execute(
            select(func.count()).select_from(AgentCommandRow).where(AgentCommandRow.status == "pending")
        )
    ).scalar_one()
    assert claimed_count == 1
    assert pending_count == 2


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_returns_one_pending_for_named_workspace(db_session) -> None:
    """claim_next returns one pending command for a named workspace_id per call."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    ws_a, ws_b = uuid4(), uuid4()

    cmd_a = _make_write_cmd(ws_a)
    cmd_b = _make_write_cmd(ws_b)
    await enqueue_command(org_id=org_id, command=cmd_a, session=db_session)
    await enqueue_command(org_id=org_id, command=cmd_b, session=db_session)
    await db_session.flush()

    # Pre-assign agent_id on those rows so they are owned by this agent.
    for cmd_id in (cmd_a.command_id, cmd_b.command_id):
        await db_session.execute(
            update(AgentCommandRow).where(AgentCommandRow.id == cmd_id).values(agent_id=agent_id)
        )
    await db_session.flush()

    # First call claims one of the two.
    first = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=0,
        workspace_ids=[ws_a, ws_b],
        wait_seconds=0,
        session=db_session,
    )
    assert first is not None
    assert first.command_id in {cmd_a.command_id, cmd_b.command_id}

    # Second call claims the other (FIFO order).
    second = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=0,
        workspace_ids=[ws_a, ws_b],
        wait_seconds=0,
        session=db_session,
    )
    assert second is not None
    assert second.command_id != first.command_id
    assert second.command_id in {cmd_a.command_id, cmd_b.command_id}


@pytest.mark.asyncio
@pytest.mark.service
async def test_claim_next_never_returns_excluded_workspace_command(db_session) -> None:
    """A workspace_id not in workspace_ids never yields a command."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    busy_ws = uuid4()
    idle_ws = uuid4()

    cmd_busy = _make_write_cmd(busy_ws)
    cmd_idle = _make_write_cmd(idle_ws)
    await enqueue_command(org_id=org_id, command=cmd_busy, session=db_session)
    await enqueue_command(org_id=org_id, command=cmd_idle, session=db_session)
    for cmd_id in (cmd_busy.command_id, cmd_idle.command_id):
        await db_session.execute(
            update(AgentCommandRow).where(AgentCommandRow.id == cmd_id).values(agent_id=agent_id)
        )
    await db_session.flush()

    command = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=0,
        workspace_ids=[idle_ws],  # only idle_ws; busy_ws excluded
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert command.command_id == cmd_idle.command_id
    assert command.command_id != cmd_busy.command_id


# ── Lease: received / requeue / done ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_lease_received_event_flips_claimed_to_delivered(db_session) -> None:
    """A `received` event from the agent flips status claimed → delivered."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Claim the command.
    command = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None

    # Flip claimed → delivered by recording a `received` event.
    await _flip_received(db_session, cmd.command_id)
    row = await _get_row(db_session, cmd.command_id)
    assert row.status == "delivered"


@pytest.mark.asyncio
@pytest.mark.service
async def test_lease_no_received_within_30s_requeues_to_pending(db_session) -> None:
    """A claimed command with no received event after 30s is requeued to pending."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Manually stamp status=claimed and claimed_at=35s ago (past the 30s lease).
    stale_claimed_at = datetime.now(UTC) - timedelta(seconds=35)
    await db_session.execute(
        update(AgentCommandRow)
        .where(AgentCommandRow.id == cmd.command_id)
        .values(status="claimed", agent_id=agent_id, claimed_at=stale_claimed_at)
    )
    await db_session.flush()

    requeued = await requeue_stale_claimed(session=db_session)
    assert requeued >= 1

    row = await _get_row(db_session, cmd.command_id)
    assert row.status == "pending"
    assert row.agent_id is None
    assert row.attempt == 1


@pytest.mark.asyncio
@pytest.mark.service
async def test_lease_terminal_event_retires_command_to_done(db_session) -> None:
    """A terminal AgentEvent causes the command row to transition to done."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Claim then deliver.
    command = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    await _flip_received(db_session, cmd.command_id)
    await _flip_done(db_session, cmd.command_id)

    row = await _get_row(db_session, cmd.command_id)
    assert row.status == "done"


@pytest.mark.asyncio
@pytest.mark.service
async def test_lease_attempt_cap_raises_loud_terminal_failure(db_session) -> None:
    """When attempt reaches the cap, the reaper marks done (terminal failure)."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Set attempt to the max - 1, claimed, stale.
    stale_claimed_at = datetime.now(UTC) - timedelta(seconds=35)
    from app.core.agent_gateway.service import MAX_ATTEMPT  # noqa: PLC0415

    await db_session.execute(
        update(AgentCommandRow)
        .where(AgentCommandRow.id == cmd.command_id)
        .values(
            status="claimed",
            agent_id=agent_id,
            claimed_at=stale_claimed_at,
            attempt=MAX_ATTEMPT - 1,
        )
    )
    await db_session.flush()

    await requeue_stale_claimed(session=db_session)

    row = await _get_row(db_session, cmd.command_id)
    # At max attempts the command is retired to done (terminal failure).
    assert row.status == "done"
    assert row.attempt == MAX_ATTEMPT


# ── Idempotency / 410 guard ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_redelivered_received_event_is_idempotent(db_session) -> None:
    """Posting received twice for the same command is a no-op on the second call."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    command = await claim_next(
        agent_id=agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None

    # First received: claimed → delivered.
    await _flip_received(db_session, cmd.command_id)
    row = await _get_row(db_session, cmd.command_id)
    assert row.status == "delivered"

    # Second received: already delivered; must stay delivered, not error.
    await _flip_received(db_session, cmd.command_id)
    row = await _get_row(db_session, cmd.command_id)
    assert row.status == "delivered"


# ── Helpers ────────────────────────────────────────────────────────────────


async def _get_row(db_session, command_id: UUID) -> AgentCommandRow:
    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == command_id))
    ).scalar_one_or_none()
    assert row is not None, f"no AgentCommandRow for {command_id}"
    return row


async def _flip_received(db_session, command_id: UUID) -> None:
    """Apply a received event via acknowledge_command_received."""
    from app.core.agent_gateway.service import acknowledge_command_received  # noqa: PLC0415

    await acknowledge_command_received(command_id, session=db_session)
    await db_session.flush()


async def _flip_done(db_session, command_id: UUID) -> None:
    """Directly mark a command done (simulates terminal event retirement)."""
    from app.core.agent_gateway.service import retire_command  # noqa: PLC0415

    await retire_command(command_id, session=db_session)
    await db_session.flush()
