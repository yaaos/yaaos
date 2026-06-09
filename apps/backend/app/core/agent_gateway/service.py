"""Durable command dispatch + event ingestion + stale-claim guard.

Commands are persisted in `agent_commands` (Postgres) and claimed via
`FOR UPDATE SKIP LOCKED` batches. A 30-second lease on `claimed` rows is
enforced by `requeue_stale_claimed`; the `cleanup_loop` in `core/workspace`
calls it on each reaper tick.

Event ingestion (`record_agent_event`) delegates the stale-claim guard lookup
to the registered `WorkspaceAgentReportSink` (owned by `core/workspace`), then
enqueues `core/workflow.handle_agent_event` via the outbox in the same
transaction when the event is terminal.

`received` is a non-terminal event: when the agent POSTs it for a claimed
command the lease is cancelled (`claimed → delivered`). Terminal events retire
the row to `done`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import structlog
from pydantic import Field, TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway.report_sink import (
    WorkspaceEventReport,
    get_report_sink,
)
from app.core.agent_gateway.types import (
    AgentCommand,
    AgentCommandKind,
    AgentConfig,
    AgentEvent,
    CleanupWorkspaceCommand,
    ConfigUpdateCommand,
    HeartbeatRequest,
    HeartbeatResponse,
    InvokeClaudeCodeCommand,
    ProvisionWorkspaceCommand,
    RefreshWorkspaceAuthCommand,
    StaleClaimError,
    WorkspaceEvent,
    WriteFilesCommand,
)
from app.core.tasks import enqueue

log = structlog.get_logger("core.agent_gateway")

# Default cap on concurrent Active workspaces per agent when no per-org
# override exists. The control plane will add per-org configuration later;
# until then all agents share this global default.
DEFAULT_MAX_WORKSPACES: int = 4

# Lease window in seconds: if a claimed command has no `received` event within
# this window it is requeued to `pending`.
LEASE_SECONDS: int = 30

# Maximum requeue attempts before a command is retired to `done` as a terminal
# failure. Prevents infinite retry of a structurally bad command.
MAX_ATTEMPT: int = 5

# Discriminated-union adapter that deserializes a persisted command payload back
# to a typed AgentCommand. Built once at import time — `claim_next` is a hot path.
_COMMAND_ADAPTER: TypeAdapter[AgentCommand] = TypeAdapter(
    Annotated[
        ProvisionWorkspaceCommand
        | WriteFilesCommand
        | RefreshWorkspaceAuthCommand
        | InvokeClaudeCodeCommand
        | CleanupWorkspaceCommand
        | ConfigUpdateCommand,
        Field(discriminator="kind"),
    ]
)


# ── Durable command queue ───────────────────────────────────────────────


async def enqueue_command(
    org_id: UUID,
    command: AgentCommand,
    *,
    session: AsyncSession,
    workflow_execution_id: UUID | None = None,
) -> None:
    """Insert an AgentCommand row in `pending` status.

    Called by the workflow engine's Workspace branch (via
    `WorkflowCommand.dispatch`) inside `start_step`'s transaction — the insert
    is atomic with the engine's state transition to `awaiting_agent`.

    `workflow_execution_id` is stamped on the row so the terminal-event
    ingestion path can resolve `command_id → workflow` directly, without a
    workspace-row lookup. NULL only for agent-scoped commands that do not
    correlate to a workflow (e.g. `ConfigUpdate`).

    The DB-minted UUIDv7 PK serves as the idempotency key and FIFO sort key.
    `agent_id` is left NULL at enqueue time; it is stamped by `claim_next`.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    # workspace_id is NULL for org-scoped commands (ConfigUpdate,
    # ProvisionWorkspace before an agent is assigned).
    workspace_id: UUID | None = getattr(command, "workspace_id", None)
    if workspace_id is not None and str(workspace_id) == "00000000-0000-0000-0000-000000000000":
        workspace_id = None

    # Override the command_id with the DB-minted UUIDv7 after flush so that
    # producers stop generating their own UUID4 ids. For now we honour the
    # caller-supplied id and treat it as the primary key.
    row = AgentCommandRow(
        id=command.command_id,
        org_id=org_id,
        workspace_id=workspace_id,
        workflow_execution_id=workflow_execution_id,
        command_kind=str(command.kind),
        payload=command.model_dump(mode="json"),
        status="pending",
    )
    session.add(row)
    await session.flush()


async def pin_command_to_agent(
    command_id: UUID,
    agent_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Pre-assign a command row to `agent_id` before it is claimed.

    Used by `dispatch_cleanup_workspace` to route post-create commands to
    the workspace's owning agent, so `claim_next`'s `workspace_ids` sweep
    can find them by `(agent_id, workspace_id, status=pending)`.
    Caller flushes/commits.
    """
    from sqlalchemy import update  # noqa: PLC0415

    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    await session.execute(
        update(AgentCommandRow).where(AgentCommandRow.id == command_id).values(agent_id=agent_id)
    )
    await session.flush()


async def get_command_org_and_payload(
    command_id: UUID,
    *,
    session: AsyncSession,
) -> tuple[UUID, dict] | None:
    """Return `(org_id, payload)` for the given `agent_commands` row, or None
    when the row is not found. Used by the workspace sink to seed the lean
    `workspaces` row on the agent's first workspace event.

    Pure read — no writes. Caller owns session lifecycle.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    row = (
        (await session.execute(select(AgentCommandRow).where(AgentCommandRow.id == command_id)))
        .scalars()
        .one_or_none()
    )
    if row is None:
        return None
    return (row.org_id, dict(row.payload) if row.payload else {})


async def get_command_workflow_execution_id(
    command_id: UUID,
    *,
    session: AsyncSession,
) -> UUID | None:
    """Return `workflow_execution_id` for the given `agent_commands` row, or
    None when the row is not found or has no workflow correlation (agent-scoped
    commands like ConfigUpdate have NULL there).

    Pure read — no writes. Used by `core/workspace` failsafe-6 to synthesize
    a terminal failure event for in-flight commands.

    Caller owns session lifecycle.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    row = (
        await session.execute(
            select(AgentCommandRow.workflow_execution_id).where(AgentCommandRow.id == command_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return row[0]


def _build_config_update() -> ConfigUpdateCommand:
    """Build a ConfigUpdateCommand from the global defaults."""
    from uuid import uuid4  # noqa: PLC0415

    return ConfigUpdateCommand(
        command_id=uuid4(),
        traceparent="",
        config=AgentConfig(
            max_workspaces=DEFAULT_MAX_WORKSPACES,
        ),
    )


def _row_to_command(row: object) -> AgentCommand:
    """Deserialize an AgentCommandRow payload back to a typed AgentCommand.

    `row` must be an `AgentCommandRow` instance; callers are responsible for
    ensuring this — the `object` annotation avoids a forward-reference at
    module level when the models import is deferred.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    assert isinstance(row, AgentCommandRow)
    return _COMMAND_ADAPTER.validate_python(row.payload)


async def claim_next(
    agent_id: UUID,
    *,
    lifecycle: str,
    new_workspaces: int,
    workspace_ids: list[UUID],
    wait_seconds: int,
    session: AsyncSession,
) -> AgentCommand | None:
    """Claim exactly one command for the agent — the highest-priority eligible row.

    Lifecycle gate:
    - `unconfigured` → return a single ConfigUpdateCommand (no DB claim).
      The pending queue is untouched so commands accumulate while the agent
      bootstraps.
    - `configured` → one `FOR UPDATE SKIP LOCKED LIMIT 1` pick across the
      eligible set (FIFO by UUIDv7 id):
        * A pending unassigned ProvisionWorkspace (status=pending, agent_id NULL,
          kind=ProvisionWorkspace), when `new_workspaces > 0`.
        * A pending command pinned to this agent for a workspace in
          `workspace_ids` (status=pending, agent_id=this agent, workspace_id ∈
          workspace_ids).
      The two sets are evaluated with a single UNION-like approach: we query
      each eligible set in priority order and take the first result, so the
      caller receives exactly one command per call. Stamps `agent_id`,
      `status=claimed`, `claimed_at=now`.

    `wait_seconds=0` → non-blocking peek (returns None immediately if nothing
    claimable). Non-zero `wait_seconds` → short-interval re-SELECT loop.
    """
    if lifecycle == "unconfigured":
        return _build_config_update()

    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    now = datetime.now(UTC)
    row: AgentCommandRow | None = None

    # Try unassigned ProvisionWorkspace first (capacity for new workspaces).
    if new_workspaces > 0:
        row = (
            (
                await session.execute(
                    select(AgentCommandRow)
                    .where(
                        AgentCommandRow.status == "pending",
                        AgentCommandRow.command_kind == AgentCommandKind.PROVISION_WORKSPACE,
                        AgentCommandRow.agent_id.is_(None),
                    )
                    .order_by(AgentCommandRow.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .one_or_none()
        )

    # If no ProvisionWorkspace, try the oldest pending command pinned to this agent
    # for any of the named workspaces.
    if row is None and workspace_ids:
        row = (
            (
                await session.execute(
                    select(AgentCommandRow)
                    .where(
                        AgentCommandRow.status == "pending",
                        AgentCommandRow.agent_id == agent_id,
                        AgentCommandRow.workspace_id.in_(workspace_ids),
                    )
                    .order_by(AgentCommandRow.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .one_or_none()
        )

    if row is None:
        if wait_seconds <= 0:
            return None
        # Long-poll: sleep in short intervals and re-try the claim SELECTs
        # until either a row is found or wait_seconds elapses.
        import asyncio  # noqa: PLC0415

        deadline = datetime.now(UTC) + timedelta(seconds=wait_seconds)
        while row is None and datetime.now(UTC) < deadline:
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            await asyncio.sleep(min(2.0, remaining))
            if datetime.now(UTC) >= deadline:
                break
            # Re-run the same claim SELECTs without recursion.
            if new_workspaces > 0:
                row = (
                    (
                        await session.execute(
                            select(AgentCommandRow)
                            .where(
                                AgentCommandRow.status == "pending",
                                AgentCommandRow.command_kind == AgentCommandKind.PROVISION_WORKSPACE,
                                AgentCommandRow.agent_id.is_(None),
                            )
                            .order_by(AgentCommandRow.id)
                            .limit(1)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .one_or_none()
                )
            if row is None and workspace_ids:
                row = (
                    (
                        await session.execute(
                            select(AgentCommandRow)
                            .where(
                                AgentCommandRow.status == "pending",
                                AgentCommandRow.agent_id == agent_id,
                                AgentCommandRow.workspace_id.in_(workspace_ids),
                            )
                            .order_by(AgentCommandRow.id)
                            .limit(1)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .one_or_none()
                )
        if row is None:
            return None

    # Stamp agent_id + claimed_at on the single selected row, and mint the
    # per-command completion capability token. We persist only the sha256 hash;
    # the raw token is returned to the claiming agent exactly once (injected into
    # the command DTO below) and never stored — bearer-token discipline applied
    # to `agent_commands`.
    raw = secrets.token_urlsafe(32)
    row.agent_id = agent_id
    row.status = "claimed"
    row.claimed_at = now
    row.completion_token_hash = hashlib.sha256(raw.encode()).hexdigest()
    await session.flush()

    # Inject the raw token into the returned DTO without re-persisting it to
    # `row.payload`. `_CommandBase` is frozen, so `model_copy(update=...)` returns
    # a new typed instance of the concrete subtype carrying the token on the wire.
    return _row_to_command(row).model_copy(update={"completion_token": raw})


async def acknowledge_command_received(
    command_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Flip a claimed command to `delivered` on receipt of a `received` event.

    Cancels the 30-second lease requeue. Idempotent: if the row is already
    `delivered` this is a no-op.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    await session.execute(
        update(AgentCommandRow)
        .where(
            AgentCommandRow.id == command_id,
            AgentCommandRow.status == "claimed",
        )
        .values(status="delivered")
    )
    await session.flush()


async def retire_command(
    command_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Retire a command to `done` status on terminal event."""
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    await session.execute(
        update(AgentCommandRow).where(AgentCommandRow.id == command_id).values(status="done")
    )
    await session.flush()


async def requeue_stale_claimed(
    *,
    session: AsyncSession,
) -> int:
    """Requeue commands that were claimed but no `received` event arrived within
    `LEASE_SECONDS`. Called each reaper tick from `core/workspace.cleanup_loop`.

    For each stale `claimed` row:
    - If `attempt < MAX_ATTEMPT`: flip back to `pending`, clear `agent_id` +
      `claimed_at`, increment `attempt`.
    - If `attempt >= MAX_ATTEMPT`: retire to `done` (loud terminal failure).

    Returns the count of rows requeued (not counting `done` retirements).
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)
    stale = (
        (
            await session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.status == "claimed",
                    AgentCommandRow.claimed_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    requeued = 0
    for row in stale:
        if row.attempt >= MAX_ATTEMPT - 1:
            # Hit the cap — retire permanently.
            row.status = "done"
            row.attempt = MAX_ATTEMPT
            log.error(
                "agent_gateway.command_attempt_cap",
                command_id=str(row.id),
                org_id=str(row.org_id),
                attempt=row.attempt,
            )
        else:
            row.status = "pending"
            row.agent_id = None
            row.claimed_at = None
            row.attempt = row.attempt + 1
            requeued += 1
            log.info(
                "agent_gateway.command_requeued",
                command_id=str(row.id),
                org_id=str(row.org_id),
                attempt=row.attempt,
            )
    if stale:
        await session.flush()
    return requeued


# ── Heartbeat / reconciliation ─────────────────────────────────────────


async def record_heartbeat(
    agent_id: UUID,
    request: HeartbeatRequest,
    *,
    session: AsyncSession,
) -> HeartbeatResponse:
    """Bump `workspace_agents.last_heartbeat_at` for the agent instance identified
    by `agent_id` and ingest workspace inventory. Returns reconciliation
    hints — workspaces the agent reports but the control plane no longer
    tracks should be torn down by the agent.

    Required `session`; caller commits.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    now = datetime.now(UTC)
    row = (
        await session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one_or_none()
    if row is not None:
        row.last_heartbeat_at = now
        row.state = "reachable"
        # Persist the count from the heartbeat payload as the single source of truth.
        # The column is populated here (not at identity exchange) because the agent
        # only knows its active workspace set at heartbeat time.
        row.claimed_workspace_count = len(request.workspaces)
    else:
        # Heartbeat arrived for an agent the control plane doesn't know about —
        # this happens transiently after a restart before identity exchange
        # writes its row, so we just log.
        log.info(
            "agent.heartbeat.unknown_agent",
            agent_id=str(agent_id),
            workspace_count=len(request.workspaces),
        )

    # Reconciliation: any workspace the agent reports that the control plane
    # has dropped (row deleted or marked `destroyed`) → tell the agent to
    # forget. Delegates to the registered sink to keep workspace-state access
    # inside core/workspace.
    reported_ids = {w.workspace_id for w in request.workspaces}
    if not reported_ids:
        return HeartbeatResponse(reconciled_at=datetime.now(UTC), forgotten_workspaces=())

    # Exclude workspace IDs that are still being provisioned (an in-flight
    # ProvisionWorkspace command exists but the workspace row hasn't been
    # created yet). The workspace row is written lazily on the first workspace
    # event from the agent; before that the row is absent, so reconciliation
    # would incorrectly mark these as forgotten and kill the subprocess mid-clone.
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    provisioning_rows = (
        await session.execute(
            select(AgentCommandRow.workspace_id).where(
                AgentCommandRow.workspace_id.in_(reported_ids),
                AgentCommandRow.command_kind == AgentCommandKind.PROVISION_WORKSPACE,
                AgentCommandRow.status.in_(["pending", "claimed", "delivered"]),
            )
        )
    ).all()
    provisioning_ids: set[UUID] = {r[0] for r in provisioning_rows if r[0] is not None}

    # Only reconcile the IDs that are not currently being provisioned.
    reconcile_ids = reported_ids - provisioning_ids

    sink = get_report_sink()
    forgotten_ids = await sink.reconcile_heartbeat(reconcile_ids, session)

    return HeartbeatResponse(
        reconciled_at=datetime.now(UTC),
        forgotten_workspaces=tuple(forgotten_ids),
    )


# ── Event ingestion ────────────────────────────────────────────────────


async def record_agent_event(
    event: AgentEvent,
    *,
    agent_id: UUID | None = None,
    session: AsyncSession,
) -> None:
    """Resolve the workflow correlation directly from `agent_commands.workflow_execution_id`,
    then — if the event is terminal — enqueue `workflow.handle_agent_event` via
    the outbox in the same transaction.

    A `received` non-terminal event flips the command row from
    `claimed` to `delivered`, cancelling the lease requeue.

    Raises `StaleClaimError` when the command row no longer exists (already
    retired by an earlier terminal event); the endpoint maps this to `410 Gone`.

    Workflow correlation is independent of the workspace row — the engine
    stamps `workflow_execution_id` on the command at enqueue time. An agent
    can therefore report a terminal event for a workspace that has been torn
    down (`failure-report-precedes-disposal`), and the workflow still resumes.

    Enforces a per-command completion-capability-token check before any side
    effect: the token minted at `claim_next` is stored as
    `agent_commands.completion_token_hash` (sha256; raw never persisted) and
    echoed back on the event's `completion_token`. The presented token is
    re-hashed and compared constant-time against the stored hash; a mismatch
    raises `StaleClaimError` (mapped to 410). Authorization binds to the COMMAND,
    not to the worker's mutable `(org_id, agent_id)` — so an agent whose identity
    legitimately rotated on re-auth still completes its in-flight command. When
    `completion_token_hash` is NULL (command never went through `claim_next`,
    e.g. test-seeded rows) verification is skipped.

    `agent_id` — the `workspace_agents.id` of the reporting bearer — is passed
    to the sink's `materialise_provision_success` when a `ProvisionWorkspace`
    command completes successfully (the Go agent never sends workspace events,
    so the lean row is materialised by the sink instead). The gateway no longer
    synthesizes a WorkspaceEvent on this path.

    Required `session`; caller commits.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415
    from app.core.agent_gateway.types import AgentEventKind  # noqa: PLC0415

    # Handle `received` before the row lookup — `received` only updates the
    # command row lease and does not require workflow correlation. It is fine
    # that this early-returning branch does not verify the completion token: it
    # bumps the lease only (no claim release, materialisation, or workflow
    # resume), and a mismatched token would simply replay through the reaper.
    if event.kind == AgentEventKind.RECEIVED:
        await acknowledge_command_received(event.command_id, session=session)
        log.info(
            "agent.event.received",
            command_id=str(event.command_id),
        )
        return

    # Resolve workflow correlation directly from the command row — no
    # workspace-row dependency for the resumption path.
    cmd_row = (
        (await session.execute(select(AgentCommandRow).where(AgentCommandRow.id == event.command_id)))
        .scalars()
        .one_or_none()
    )
    if cmd_row is None:
        raise StaleClaimError(f"no agent_commands row for {event.command_id}")

    # Completion-capability-token check — the churn-proof replacement for the
    # org/agent ownership guard. Authorization binds to the COMMAND via the
    # one-time token minted at claim, not to the worker's mutable identity (which
    # legitimately rotates on re-auth). Run BEFORE any claim release, run-sink
    # call, lean-row materialisation, or workflow enqueue. Constant-time compare;
    # the token is never logged. Skipped when the command never went through
    # `claim_next` (NULL hash — e.g. test-seeded rows).
    if cmd_row.completion_token_hash is not None:
        presented = hashlib.sha256((event.completion_token or "").encode()).hexdigest()
        if not hmac.compare_digest(presented, cmd_row.completion_token_hash):
            raise StaleClaimError(f"command {event.command_id} completion token mismatch")

    holder_workflow_id = cmd_row.workflow_execution_id

    if not event.is_terminal():
        # Non-terminal events (progress) skip workflow-engine resumption —
        # only `completed_*` events resume the workflow state machine.
        # Republish to the org-scoped workspace-activity channel so the SPA's
        # SSE live-tail picks them up. Skipped when the command has no
        # workflow correlation (agent-scoped ConfigUpdate has no live-tail
        # subscriber to fan out to).
        log.info(
            "agent.event.progress",
            command_id=str(event.command_id),
        )
        if holder_workflow_id is not None:
            from app.core.auth import require_org_context  # noqa: PLC0415
            from app.core.sse import publish_workspace_activity  # noqa: PLC0415

            await publish_workspace_activity(
                org_id=require_org_context(),
                workflow_execution_id=holder_workflow_id,
                payload=event.model_dump(mode="json"),
            )
        return

    # Terminal — release the single-flight workspace claim BEFORE routing to
    # the next step or finalizer, so the next `try_claim` sees
    # `current_command_id IS NULL` (failure-report-precedes-disposal).
    # No-op when no workspace row holds this command (e.g. ProvisionWorkspace
    # before the lean row exists, or agent-scoped commands).
    await get_report_sink().release_command_claim(event.command_id, session)

    # Retire the command row and enqueue the workflow handler
    # (only when there is a workflow to resume; agent-scoped commands without
    # workflow correlation simply retire).
    await retire_command(event.command_id, session=session)

    # Fan out to the coding-agent run sink — only `InvokeClaudeCode` terminal
    # events need a run row finalized. The sink filters on command_kind and
    # is a no-op for all other kinds. The sink is optional (None when
    # domain/coding_agent is not loaded), so it degrades gracefully.
    from app.core.agent_gateway.run_sink import get_run_sink  # noqa: PLC0415

    _run_sink = get_run_sink()
    if _run_sink is not None:
        await _run_sink.handle_terminal_event(
            command_id=event.command_id,
            command_kind=cmd_row.command_kind,
            event_kind=event.kind.value,
            outputs=dict(event.outputs),
            session=session,
        )

    # Lean workspace row materialisation for ProvisionWorkspace.
    #
    # The Go agent never sends workspace events (WorkspaceEvent is a
    # backend-side type — see openapi_drift_test.go). The control plane
    # therefore materialises the row on the terminal `completed_success` for
    # the ProvisionWorkspace command. The gateway does not synthesize a
    # WorkspaceEvent or pick a "kind"; it delegates to the sink, which owns all
    # workspace-state shaping (provider id, TTL, spec). The sink is idempotent
    # — a row already present is left untouched.
    if (
        agent_id is not None
        and cmd_row.command_kind == AgentCommandKind.PROVISION_WORKSPACE
        and cmd_row.workspace_id is not None
        and event.kind == AgentEventKind.COMPLETED_SUCCESS
    ):
        await get_report_sink().materialise_provision_success(
            command_id=event.command_id,
            agent_id=agent_id,
            session=session,
        )

    if holder_workflow_id is None:
        return

    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": str(holder_workflow_id),
            "agent_command_id": str(event.command_id),
            "outcome_label": event.outcome_label
            or ("failure" if event.kind == AgentEventKind.COMPLETED_FAILURE else "success"),
            "outputs": dict(event.outputs),
            "traceparent": event.traceparent,
        },
        session=session,
    )


async def record_workspace_event(
    event: WorkspaceEvent,
    *,
    agent_id: UUID | None = None,
    session: AsyncSession,
) -> None:
    """Update the workspace mirror from an agent-reported state change.

    Delegates all workspace-state access to the registered sink. The sink
    applies the stale-claim guard and the kind→status map, returning an
    outcome VO. agent_gateway maps `accepted=False` to `StaleClaimError`
    so the endpoint can return `410 Gone`.

    `agent_id` is the bearer's `WorkspaceAgentRow.id`. Passed to the sink so
    lean row creation (on the agent's first workspace event) can stamp
    `owning_agent_id` correctly.
    """
    sink = get_report_sink()
    report = WorkspaceEventReport(
        workspace_id=event.workspace_id,
        command_id=event.command_id,
        kind=event.kind,
        agent_id=agent_id,
    )
    outcome = await sink.apply_workspace_event(report, session)
    if not outcome.accepted:
        raise StaleClaimError(
            f"workspace {event.workspace_id} rejected event {event.kind!r} (command {event.command_id})"
        )
    log.info(
        "agent.workspace_event",
        workspace_id=str(event.workspace_id),
        kind=event.kind,
        new_status=outcome.resolved_status,
    )


# ── Identity-exchange writer + connection status ───────────────────────


async def ensure_agent_row(
    *,
    org_id: UUID,
    instance_id: str,
    iam_arn: str,
    version: str | None,
    session: AsyncSession,
    os: str | None = None,
    cpu_count: int | None = None,
    memory_bytes: int | None = None,
) -> UUID:
    """Insert or update the `workspace_agents` row for `(org_id, instance_id)`
    on a successful identity exchange. Returns the row's `id` — this is
    the `agent_id` the bearer is scoped to and that subsequent endpoints
    use to address the agent instance.

    `instance_id` is the role-session-name derived from the STS assumed-role ARN.
    Stable across agent restarts when the ECS task reuses the same session name.

    Caller commits.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    row = (
        await session.execute(
            select(WorkspaceAgentRow).where(
                WorkspaceAgentRow.org_id == org_id,
                WorkspaceAgentRow.instance_id == instance_id,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = WorkspaceAgentRow(
            org_id=org_id,
            instance_id=instance_id,
            iam_arn=iam_arn,
            version=version,
            os=os,
            cpu_count=cpu_count,
            memory_bytes=memory_bytes,
            last_heartbeat_at=now,
            state="reachable",
        )
        session.add(row)
        await session.flush()
    else:
        row.iam_arn = iam_arn
        row.version = version
        # Update static metadata on re-exchange (agent restart may report fresh values).
        if os is not None:
            row.os = os
        if cpu_count is not None:
            row.cpu_count = cpu_count
        if memory_bytes is not None:
            row.memory_bytes = memory_bytes
        row.last_heartbeat_at = now
        row.state = "reachable"
    return row.id


async def mark_agent_shutdown(
    agent_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Set `state=offline` + `last_shutdown_at=now` on the agent row.

    Called by the graceful-shutdown DELETE handler immediately before revoking
    bearers + triggering workspace cleanup. Caller commits.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    now = datetime.now(UTC)
    row = (
        await session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one_or_none()
    if row is not None:
        row.state = "offline"
        row.last_shutdown_at = now
        await session.flush()


async def get_agent_info(
    agent_id: UUID,
    *,
    session: AsyncSession,
) -> dict | None:
    """Return a plain dict snapshot of the agent row, or None if absent.

    Keys: `id`, `org_id`, `instance_id`, `iam_arn`, `version`, `state`,
    `last_heartbeat_at`. Exists so cross-module tests can verify agent state
    without importing the Row class.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    row = await session.get(WorkspaceAgentRow, agent_id)
    if row is None:
        return None
    return {
        "id": row.id,
        "org_id": row.org_id,
        "instance_id": row.instance_id,
        "iam_arn": row.iam_arn,
        "version": row.version,
        "state": row.state,
        "last_heartbeat_at": row.last_heartbeat_at,
    }


async def has_any_reachable_agent(
    *,
    session: AsyncSession,
) -> bool:
    """Return `True` when at least one workspace agent instance heartbeated
    within the last 90 s — used by health-check callers to avoid cross-module
    Row access.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(seconds=90)
    rows = (
        (
            await session.execute(
                select(WorkspaceAgentRow.id)
                .where(
                    WorkspaceAgentRow.state == "reachable",
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                    WorkspaceAgentRow.last_heartbeat_at >= cutoff,
                )
                .limit(1)
            )
        )
        .tuples()
        .all()
    )
    return bool(rows)


async def connection_status_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> dict[str, object]:
    """Aggregate `workspace_agents` for `org_id`. Returns:
    `{state, pod_count, latest_heartbeat_at}` where `state` is one of:

    - `connected` — at least one agent instance heartbeated within the last 90s
    - `lost` — at least one row exists but none recent enough
    - `not_configured` — no rows at all for this org

    `pod_count` is the number of known agent instances; the key name is
    preserved for wire compatibility.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    rows = (
        (await session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.org_id == org_id)))
        .scalars()
        .all()
    )
    if not rows:
        return {"state": "not_configured", "pod_count": 0, "latest_heartbeat_at": None}
    latest = max((r.last_heartbeat_at for r in rows if r.last_heartbeat_at is not None), default=None)
    cutoff = datetime.now(UTC) - timedelta(seconds=90)
    state = "connected" if latest is not None and latest >= cutoff else "lost"
    return {
        "state": state,
        "pod_count": len(rows),
        "latest_heartbeat_at": latest.isoformat() if latest is not None else None,
    }


async def stale_agent_ids(
    agent_ids: set[UUID],
    *,
    cutoff: datetime,
    session: AsyncSession,
) -> set[UUID]:
    """Return the subset of `agent_ids` that are individually stale — no
    `last_heartbeat_at` at or after *cutoff* (or never heartbeated, or no row).

    Used by `core/workspace` failsafe-6 to expire only the workspaces whose
    owning agent is lost, leaving healthy sibling agent instances' workspaces
    untouched — without importing `workspace_agents` directly.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    if not agent_ids:
        return set()
    fresh = (
        (
            await session.execute(
                select(WorkspaceAgentRow.id).where(
                    WorkspaceAgentRow.id.in_(agent_ids),
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                    WorkspaceAgentRow.last_heartbeat_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    return agent_ids - set(fresh)


# Liveness thresholds (seconds since last heartbeat).
_STALE_THRESHOLD_SECONDS: int = 60  # reachable → stale
_OFFLINE_THRESHOLD_SECONDS: int = 5 * 60  # reachable/stale → offline
_UI_RETENTION_SECONDS: int = 60 * 60  # agents older than this are hidden from the dashboard


async def compute_agent_liveness_transitions(
    now: datetime,
    *,
    session: AsyncSession,
) -> list[UUID]:
    """Compute and apply liveness-state transitions for all workspace-agent rows.

    State machine (based on seconds since `last_heartbeat_at`):
    - ``< 60 s`` → reachable (online)
    - ``60 s - 5 min`` → stale
    - ``> 5 min`` or explicit shutdown (last_shutdown_at is set and agent is not
      reachable) → offline

    Writes `state` only when a transition occurs — idempotent on the same tick.
    Returns the list of agent UUIDs that newly became offline on this sweep.
    Emits one ``agent_liveness_changed`` SSE event per transitioned agent via
    ``publish_general_after_commit`` so the dashboard invalidates live.

    Lives in ``core/agent_gateway`` because it owns the ``workspace_agents``
    table; called each reaper tick from ``core/workspace`` (which can import
    ``core/agent_gateway`` per the tach boundary).
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    # Exclude agents already offline (shutdowns are permanent until re-exchange).
    rows = (
        (
            await session.execute(
                select(WorkspaceAgentRow).where(
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    newly_offline: list[UUID] = []

    for row in rows:
        if row.last_heartbeat_at is None:
            continue
        age_seconds = (now - row.last_heartbeat_at).total_seconds()

        if age_seconds > _OFFLINE_THRESHOLD_SECONDS:
            target_state = "offline"
        elif age_seconds > _STALE_THRESHOLD_SECONDS:
            target_state = "stale"
        else:
            target_state = "reachable"

        if row.state == target_state:
            continue  # No transition — skip write and SSE.

        prev_state = row.state
        row.state = target_state
        await session.flush()

        if target_state == "offline":
            newly_offline.append(row.id)

        log.info(
            "agent_gateway.liveness_transition",
            agent_id=str(row.id),
            org_id=str(row.org_id),
            from_state=prev_state,
            to_state=target_state,
        )

        publish_general_after_commit(
            session,
            org_id=row.org_id,
            kind=GeneralEventKind.AGENT_LIVENESS_CHANGED,
            payload={},
        )

    return newly_offline


async def list_agents_for_org(
    org_id: UUID,
    *,
    now: datetime,
    session: AsyncSession,
) -> list[dict]:
    """Return agent rows for `org_id` within the 1-hour UI-retention window.

    Each dict contains the fields the dashboard ``AgentCard`` displays:
    ``id``, ``instance_id``, ``state``, ``last_heartbeat_at``, ``os``,
    ``cpu_count``, ``memory_bytes``, ``claimed_workspace_count``, ``version``.

    Agents whose last heartbeat (or last shutdown) is older than 1 hour are
    excluded — the row stays in the DB but the dashboard stops showing it.
    DB rows are never deleted by this path.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    retention_cutoff = now - timedelta(seconds=_UI_RETENTION_SECONDS)
    rows = (
        (
            await session.execute(
                select(WorkspaceAgentRow).where(
                    WorkspaceAgentRow.org_id == org_id,
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                    WorkspaceAgentRow.last_heartbeat_at >= retention_cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "id": row.id,
            "instance_id": row.instance_id,
            "state": row.state,
            "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
            "os": row.os,
            "cpu_count": row.cpu_count,
            "memory_bytes": row.memory_bytes,
            "claimed_workspace_count": row.claimed_workspace_count,
            "version": row.version,
        }
        for row in rows
    ]
