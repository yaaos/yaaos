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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

if TYPE_CHECKING:
    from app.core.audit_log import Actor
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field, RootModel, TypeAdapter
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    CancelShutdownCommand,
    CleanupWorkspaceCommand,
    ConfigUpdateCommand,
    HeartbeatRequest,
    HeartbeatResponse,
    InvokeClaudeCodeCommand,
    ProvisionWorkspaceCommand,
    RefreshWorkspaceAuthCommand,
    ShutdownCommand,
    StaleClaimError,
    WorkspaceEvent,
    WriteFilesCommand,
)
from app.core.observability import current_traceparent
from app.core.tasks import enqueue

log = structlog.get_logger("core.agent_gateway")
_tracer = trace.get_tracer(__name__)

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
        | ConfigUpdateCommand
        | ShutdownCommand
        | CancelShutdownCommand,
        Field(discriminator="kind"),
    ]
)


# ── Durable command queue ───────────────────────────────────────────────

# Envelope keys that are gateway-owned and must never come from a caller's
# payload_fields. Stripped from the full command dump in `enqueue_command`
# so only kind-specific keys flow through `enqueue_command_payload`.
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "kind",
        "command_id",
        "workspace_id",
        "traceparent",
        "completion_token",
        "workflow_execution_id",
    }
)


async def enqueue_command_payload(
    org_id: UUID,
    *,
    command_id: UUID,
    kind: AgentCommandKind | str,
    workspace_id: UUID | None,
    payload_fields: BaseModel,
    session: AsyncSession,
    traceparent: str | None = None,
    workflow_execution_id: UUID | None = None,
) -> None:
    """Insert an AgentCommand row in `pending` status from typed payload fields.

    The typed-fields counterpart to `enqueue_command`. Callers that build
    wire payloads from first principles (e.g. `core/coding_agent`) use this to
    avoid importing any vendor-specific AgentCommand subclass.

    `payload_fields` carries only the command-kind-specific fields — no envelope
    keys. The envelope (`kind`, `command_id`, `workspace_id`, `traceparent`,
    `completion_token`, `workflow_execution_id`) is built from the named parameters
    and merged LAST, so identity fields can never be overwritten by the caller.

    Merge order: `{**payload_fields.model_dump(mode="json"), **envelope}`.
    The envelope's `traceparent` is unconditionally set to the dispatch span's
    own traceparent (via `current_traceparent()`), making `enqueue_command_payload`
    the sole owner of that field on the wire.

    The caller-supplied `command_id` becomes the row PK and the FIFO sort key.
    Producers must mint it with `uuid7()` so claim order matches enqueue order.

    Opens an `agent_command.dispatch.{kind}` OTel span covering the full insert.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    kind_str = str(kind)
    with _tracer.start_as_current_span(f"agent_command.dispatch.{kind_str}") as span:
        span.set_attribute("kind", kind_str)
        span.set_attribute("command_id", str(command_id))
        span.set_attribute("workspace_id", str(workspace_id) if workspace_id is not None else "")
        span.set_attribute(
            "workflow_id",
            str(workflow_execution_id) if workflow_execution_id is not None else "",
        )
        try:
            # Overwrite the caller-supplied traceparent with the dispatch span's
            # own traceparent so the agent's supervisor.dispatch.<kind> span is
            # parented to agent_command.dispatch.<kind>, not the outer caller's
            # span. This function is the sole owner of this field on the wire.
            dispatch_tp = current_traceparent()
            effective_tp = dispatch_tp if dispatch_tp is not None else (traceparent or "")
            # Build the envelope from named parameters. These are the gateway-owned
            # identity fields — they must always win over anything the caller-supplied
            # payload_fields could produce. completion_token is NULL at enqueue time;
            # `claim_next` injects the raw token into the returned DTO without
            # re-persisting, so it is never stored here.
            # workspace_id is omitted when None (ConfigUpdateCommand has no workspace_id
            # field in its discriminated-union shape).
            envelope: dict[str, Any] = {
                "kind": kind_str,
                "command_id": str(command_id),
                "traceparent": effective_tp,
                "completion_token": None,
                "workflow_execution_id": str(workflow_execution_id)
                if workflow_execution_id is not None
                else None,
            }
            if workspace_id is not None:
                envelope["workspace_id"] = str(workspace_id)
            # Merge: kind-specific fields first, envelope LAST — so identity fields
            # (kind, command_id, traceparent, completion_token, workflow_execution_id,
            # workspace_id) can never be overwritten by the caller's payload_fields.
            final_payload = {**payload_fields.model_dump(mode="json"), **envelope}
            # The caller-supplied command_id is the row PK and FIFO sort key.
            # Producers mint it with uuid7() so it is time-ordered (see docstring).
            row = AgentCommandRow(
                id=command_id,
                org_id=org_id,
                workspace_id=workspace_id,
                workflow_execution_id=workflow_execution_id,
                command_kind=kind_str,
                payload=final_payload,
                status="pending",
            )
            session.add(row)
            await session.flush()
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


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

    The caller-supplied command_id becomes the row PK; it serves as the
    idempotency key and the FIFO sort key (`claim_next` orders by `id`). Producers
    mint it with `uuid7()` so the PK is time-ordered and claim order matches
    enqueue order — a random `uuid4` would scramble FIFO delivery. The column's
    `server_default=text("uuidv7()")` is the fallback for the rare insert that
    omits `id`. `agent_id` is left NULL at enqueue time; it is stamped by
    `claim_next`.

    Opens an `agent_command.dispatch.{kind}` OTel span covering the full insert.
    `org_id`/`actor_kind`/`workflow_id` are auto-stamped by the
    `YaaosDimensionsSpanProcessor`. `command_id` and `workspace_id` are set
    explicitly here since they are command-scoped (not process-wide dimensions).

    Thin wrapper around `enqueue_command_payload` — unpacks the typed command
    into primitives and delegates. Retained for the workspace-lifecycle callers
    that already have typed `AgentCommand` instances.
    """
    kind = str(command.kind)
    workspace_id: UUID | None = getattr(command, "workspace_id", None)
    if workspace_id is not None and str(workspace_id) == "00000000-0000-0000-0000-000000000000":
        workspace_id = None
    # Dump the full command and strip envelope keys so `enqueue_command_payload`
    # receives only the kind-specific fields. The envelope fields (kind, command_id,
    # workspace_id, traceparent, completion_token, workflow_execution_id) are
    # re-injected by `enqueue_command_payload` from the named parameters —
    # ensuring the gateway always owns those identity fields.
    full_dump = command.model_dump(mode="json")
    kind_fields = {k: v for k, v in full_dump.items() if k not in _ENVELOPE_KEYS}
    payload_fields: BaseModel = RootModel[dict[str, Any]](kind_fields)
    traceparent = getattr(command, "traceparent", "") or ""
    await enqueue_command_payload(
        org_id,
        command_id=command.command_id,
        kind=kind,
        workspace_id=workspace_id,
        payload_fields=payload_fields,
        traceparent=traceparent,
        session=session,
        workflow_execution_id=workflow_execution_id,
    )


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


async def _build_config_update_dto(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> ConfigUpdateCommand:
    """Construct a ConfigUpdateCommand DTO from current Settings + per-org BYOK secrets.

    No DB side effect of its own — the byok provider may read from the DB via the
    supplied session. Used by `enqueue_config_update_for_agent` and by tests
    that need to inspect the settings → AgentConfig mapping without going
    through the claim channel.
    """
    from uuid import uuid7  # noqa: PLC0415

    from app.core.agent_gateway.byok_provider import get_byok_secrets_provider  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    byok_secrets: dict = {}
    provider = get_byok_secrets_provider()
    if provider is not None:
        byok_secrets = await provider(org_id, session=session)
    return ConfigUpdateCommand(
        command_id=uuid7(),
        traceparent="",
        config=AgentConfig(
            max_workspaces=DEFAULT_MAX_WORKSPACES,
            otlp_endpoint=settings.yaaos_dash0_endpoint,
            otlp_token=settings.yaaos_agent_dash0_bearer_token,
            otlp_dataset=settings.yaaos_dash0_dataset,
            environment=settings.environment,
            byok_secrets=byok_secrets,
        ),
    )


async def enqueue_config_update_for_agent(
    agent_id: UUID,
    *,
    org_id: UUID,
    session: AsyncSession,
) -> None:
    """Insert a ConfigUpdate command row for the given agent in the caller's transaction.

    Called during identity exchange alongside `ensure_agent_row` so the agent
    can claim its runtime configuration via the normal FIFO claim path. Enqueues
    unconditionally — duplicate ConfigUpdate rows are harmless since `ApplyConfig`
    is idempotent (last-write-wins on the agent side). The row gets a real PK
    (UUIDv7), is pre-stamped with `agent_id` so the unconfigured claim SELECT
    finds it without a `workspace_ids` sweep, and carries a non-null
    `completion_token_hash` placeholder — `claim_next` overwrites the hash with
    a freshly-minted one before returning to the agent. The placeholder keeps
    the row outside the `completion_token_hash IS NULL` carve-out used only by
    test-seeded rows.

    Caller commits.
    """
    cmd = await _build_config_update_dto(org_id, session=session)
    # Pre-stamp the agent_id so the unconfigured claim SELECT finds it without
    # a workspace_ids sweep. `enqueue_command` leaves agent_id NULL; we patch it
    # immediately after — also setting a placeholder `completion_token_hash`
    # (claim-time mint overwrites it).
    await enqueue_command(org_id=org_id, command=cmd, session=session)
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    placeholder_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
    await session.execute(
        update(AgentCommandRow)
        .where(AgentCommandRow.id == cmd.command_id)
        .values(agent_id=agent_id, completion_token_hash=placeholder_hash)
    )
    await session.flush()


async def enqueue_config_update_for_all_org_agents(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Insert a ConfigUpdate command row for every reachable agent in the org.

    Used to fan-out a BYOK key change (set or clear) to all currently-registered
    agents so each agent's next claim picks up the updated byok_secrets. Agents
    that are not yet configured (no prior ConfigUpdate) are also notified.

    Caller commits. No-op when the org has no agents.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    rows = (
        (await session.execute(select(WorkspaceAgentRow.id).where(WorkspaceAgentRow.org_id == org_id)))
        .scalars()
        .all()
    )
    for agent_id in rows:
        await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=session)


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
    - `unconfigured` → one `FOR UPDATE SKIP LOCKED LIMIT 1` pick across
      pending ConfigUpdate rows pre-stamped to this agent (FIFO by UUIDv7 id).
      Returns None when no ConfigUpdate row is pending for this agent. The rest
      of the queue is untouched so non-ConfigUpdate commands accumulate while
      the agent bootstraps.
    - `configured` → one `FOR UPDATE SKIP LOCKED LIMIT 1` pick across the
      eligible set, evaluated in priority order (FIFO by UUIDv7 id within each):
        * A pending ConfigUpdate pinned to this agent — runs FIRST so credential
          and endpoint rotations (BYOK keys, OTLP token) land before any
          ProvisionWorkspace injects per-workspace env that lives for the
          workspace's whole life.
        * A pending unassigned ProvisionWorkspace (status=pending, agent_id NULL,
          kind=ProvisionWorkspace), when `new_workspaces > 0`.
        * A pending command pinned to this agent for a workspace in
          `workspace_ids` (status=pending, agent_id=this agent, workspace_id ∈
          workspace_ids).
      The three sets are evaluated with a single UNION-like approach: we query
      each eligible set in priority order and take the first result, so the
      caller receives exactly one command per call. Stamps `agent_id`,
      `status=claimed`, `claimed_at=now`.

    `wait_seconds=0` → non-blocking peek (returns None immediately if nothing
    claimable). Non-zero `wait_seconds` → short-interval re-SELECT loop.
    """
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    now = datetime.now(UTC)
    row: AgentCommandRow | None = None

    if lifecycle == "unconfigured":
        # Unconfigured agents may only claim their pending ConfigUpdate row.
        # `enqueue_config_update_for_agent` pre-stamps `agent_id` so this
        # SELECT finds it without a workspace_ids sweep.
        row = (
            (
                await session.execute(
                    select(AgentCommandRow)
                    .where(
                        AgentCommandRow.status == "pending",
                        AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                        AgentCommandRow.agent_id == agent_id,
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
            import asyncio  # noqa: PLC0415

            deadline = datetime.now(UTC) + timedelta(seconds=wait_seconds)
            while row is None and datetime.now(UTC) < deadline:
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                await asyncio.sleep(min(2.0, remaining))
                if datetime.now(UTC) >= deadline:
                    break
                row = (
                    (
                        await session.execute(
                            select(AgentCommandRow)
                            .where(
                                AgentCommandRow.status == "pending",
                                AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                                AgentCommandRow.agent_id == agent_id,
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
    else:
        # Agent-scoped priority bucket first: ConfigUpdate, Shutdown, and
        # CancelShutdown are pre-stamped with agent_id and carry no workspace_id.
        # ConfigUpdate carries credential / endpoint rotations (BYOK keys, OTLP
        # token) that must land before the next ProvisionWorkspace. Shutdown /
        # CancelShutdown are agent lifecycle signals that must land regardless of
        # workspace capacity.
        row = (
            (
                await session.execute(
                    select(AgentCommandRow)
                    .where(
                        AgentCommandRow.status == "pending",
                        AgentCommandRow.command_kind.in_(
                            [
                                AgentCommandKind.CONFIG_UPDATE,
                                AgentCommandKind.SHUTDOWN,
                                AgentCommandKind.CANCEL_SHUTDOWN,
                            ]
                        ),
                        AgentCommandRow.agent_id == agent_id,
                    )
                    .order_by(AgentCommandRow.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .one_or_none()
        )

        # Then unassigned ProvisionWorkspace (capacity for new workspaces).
        if row is None and new_workspaces > 0:
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
                row = (
                    (
                        await session.execute(
                            select(AgentCommandRow)
                            .where(
                                AgentCommandRow.status == "pending",
                                AgentCommandRow.command_kind.in_(
                                    [
                                        AgentCommandKind.CONFIG_UPDATE,
                                        AgentCommandKind.SHUTDOWN,
                                        AgentCommandKind.CANCEL_SHUTDOWN,
                                    ]
                                ),
                                AgentCommandRow.agent_id == agent_id,
                            )
                            .order_by(AgentCommandRow.id)
                            .limit(1)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .one_or_none()
                )
                if row is None and new_workspaces > 0:
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

    # Inject the raw token and workflow_execution_id into the returned DTO without
    # re-persisting them to `row.payload`. `_CommandBase` is frozen, so
    # `model_copy(update=...)` returns a new typed instance of the concrete subtype.
    # workflow_execution_id is read from the row's dedicated column (not the payload)
    # so agent-side spans can carry workflow_id without a separate lookup.
    updates: dict = {"completion_token": raw}
    if row.workflow_execution_id is not None:
        updates["workflow_execution_id"] = row.workflow_execution_id
    return _row_to_command(row).model_copy(update=updates)


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
                select(AgentCommandRow)
                .where(
                    AgentCommandRow.status == "claimed",
                    AgentCommandRow.claimed_at < cutoff,
                )
                .with_for_update(skip_locked=True)
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
            log.debug(
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
        # Publish agent_changed after commit so the SPA's agents card updates
        # claimed_workspace_count in near-real-time (30s normal cadence; 5s
        # during drain). One Redis pub/sub per heartbeat — negligible load.
        from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

        publish_general_after_commit(
            session,
            org_id=row.org_id,
            kind=GeneralEventKind.AGENT_CHANGED,
            payload={"agent_id": str(row.id)},
        )
    else:
        # Heartbeat arrived for an agent the control plane doesn't know about —
        # this happens transiently after a restart before identity exchange
        # writes its row, so we just log.
        log.debug(
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
    retired by an earlier terminal event); the endpoint maps it to `410 Gone`
    with `{"error": "stale_claim"}`.

    Workflow correlation is independent of the workspace row — the engine
    stamps `workflow_execution_id` on the command at enqueue time. An agent
    can therefore report a terminal event for a workspace that has been torn
    down (`failure-report-precedes-disposal`), and the workflow still resumes.

    Enforces a per-command completion-capability-token check before any side
    effect: the token minted at `claim_next` is stored as
    `agent_commands.completion_token_hash` (sha256; raw never persisted) and
    echoed back on the event's `completion_token`. The presented token is
    re-hashed and compared constant-time against the stored hash; a mismatch
    raises `StaleClaimError` (the endpoint returns `410 Gone`). Authorization binds to the COMMAND,
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

    # `received` only updates the command row lease and does not require workflow
    # correlation — exit after the integrity gate so mismatched tokens are rejected.
    if event.kind == AgentEventKind.RECEIVED:
        await acknowledge_command_received(event.command_id, session=session)
        log.debug(
            "agent.event.received",
            command_id=str(event.command_id),
        )
        return

    holder_workflow_id = cmd_row.workflow_execution_id

    if not event.is_terminal():
        # Non-terminal events (progress) skip workflow-engine resumption —
        # only `completed_*` events resume the workflow state machine.
        # Republish to the org-scoped workspace-activity channel so the SPA's
        # SSE live-tail picks them up. Skipped when the command has no
        # workflow correlation (agent-scoped ConfigUpdate has no live-tail
        # subscriber to fan out to).
        log.debug(
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

    # ConfigUpdate completed_success → CAS-flip lifecycle unconfigured → active.
    # Must run BEFORE release_command_claim so the row is stable before any
    # downstream SSE. CAS is a no-op if lifecycle is already active/draining/shutdown
    # (see Invariant — ConfigUpdate during drain in architecture.md).
    if (
        event.kind == AgentEventKind.COMPLETED_SUCCESS
        and cmd_row.command_kind == AgentCommandKind.CONFIG_UPDATE
        and cmd_row.agent_id is not None
    ):
        await mark_agent_configured(agent_id=cmd_row.agent_id, session=session)

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
    # is a no-op for all other kinds. Presence is structurally guaranteed by
    # the boot-time assert in web.py and worker.py.
    from app.core.agent_gateway.run_sink import AgentEventEnrichment, get_run_sink  # noqa: PLC0415

    outputs: dict = dict(event.outputs)  # type: ignore[type-arg]
    sink = get_run_sink()
    assert sink is not None, "run sink must be registered (asserted at boot)"
    sink_extras: AgentEventEnrichment | None = await sink.handle_terminal_event(
        command_id=event.command_id,
        command_kind=cmd_row.command_kind,
        event_kind=event.kind.value,
        outputs=outputs,
        session=session,
    )
    if sink_extras is not None:
        outputs = {**outputs, **sink_extras}

    # Strip raw agent `stdout` after the sink has processed it. The sink is
    # the source of truth for what flows forward — it returns `{"output": ...}`
    # (the structured skill response JSON extracted from the stream-json result field)
    # so `CodingAgentCommand.handle_response` can directly validate it.
    # Leaving `stdout` in the forwarded dict would allow stale reads of the
    # old key from any future step that accidentally used it.
    outputs.pop("stdout", None)

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
            "outputs": outputs,
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

    Both event endpoints share the same stale-claim contract: the sibling
    `record_agent_event` (terminal events on the command-event endpoint) also
    returns `410 Gone` on a missing/retired command row — every 410 on either
    endpoint is a real stale-claim, paging-worthy in steady state.

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
    log.debug(
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

    now = datetime.now(UTC)
    # Single atomic upsert — idempotent under concurrent identity exchanges for
    # the same (org_id, instance_id).  Optional hardware metadata uses
    # coalesce(excluded, current) so a re-exchange that omits os/cpu/memory
    # preserves the stored values rather than wiping them.
    insert_stmt = pg_insert(WorkspaceAgentRow).values(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn=iam_arn,
        version=version,
        os=os,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
        last_heartbeat_at=now,
        state="reachable",
        # lifecycle DEFAULT 'unconfigured' applies on INSERT (column-level DEFAULT).
        # The UPSERT set_ clause below handles the CONFLICT branch.
    )
    exc = insert_stmt.excluded
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=["org_id", "instance_id"],
        set_={
            "iam_arn": exc.iam_arn,
            "version": exc.version,
            "os": func.coalesce(exc.os, WorkspaceAgentRow.os),
            "cpu_count": func.coalesce(exc.cpu_count, WorkspaceAgentRow.cpu_count),
            "memory_bytes": func.coalesce(exc.memory_bytes, WorkspaceAgentRow.memory_bytes),
            "last_heartbeat_at": exc.last_heartbeat_at,
            "state": exc.state,
            # Preserve lifecycle across bearer-refresh (a re-exchange while
            # draining must not reset backend lifecycle="draining" to unconfigured).
            # Exception: reconnect of a previously-shutdown agent resets to
            # unconfigured — treat it as a fresh agent.
            "lifecycle": case(
                (WorkspaceAgentRow.lifecycle == "shutdown", "unconfigured"),
                else_=WorkspaceAgentRow.lifecycle,
            ),
        },
    ).returning(WorkspaceAgentRow.id, WorkspaceAgentRow.org_id)
    result_row = (await session.execute(upsert_stmt)).one()
    agent_id_val: UUID = result_row[0]
    agent_org_id: UUID = result_row[1]

    # Publish agent_changed after commit so the SPA picks up boot/reconnect
    # events unconditionally — no payload diffing needed; the SPA refetches.
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    publish_general_after_commit(
        session,
        org_id=agent_org_id,
        kind=GeneralEventKind.AGENT_CHANGED,
        payload={"agent_id": str(agent_id_val)},
    )

    return agent_id_val


async def mark_agent_offline(
    agent_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Set `state=offline` + `last_shutdown_at=now` on the agent row.

    Writes liveness state only — never touches `lifecycle`.  Called by the
    graceful-shutdown DELETE handler immediately before revoking bearers +
    triggering workspace cleanup. Caller commits.
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


async def mark_agent_configured(
    *,
    agent_id: UUID,
    session: AsyncSession,
) -> None:
    """Atomic CAS: `lifecycle='active' WHERE id=? AND lifecycle='unconfigured'`.

    Called by `record_agent_event` on a ConfigUpdate `completed_success`.
    No-op when `lifecycle` is already `active`, `draining`, or `shutdown` —
    applying a config never overrides an in-progress drain.  When the CAS
    flips a row, publishes `agent_changed` after commit.

    Caller commits.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    result = await session.execute(
        update(WorkspaceAgentRow)
        .where(
            WorkspaceAgentRow.id == agent_id,
            WorkspaceAgentRow.lifecycle == "unconfigured",
        )
        .values(lifecycle="active")
        .returning(WorkspaceAgentRow.org_id)
    )
    org_id_val = result.scalar_one_or_none()
    if org_id_val is not None:
        # CAS won — publish SSE so the SPA card refreshes immediately.
        publish_general_after_commit(
            session,
            org_id=org_id_val,
            kind=GeneralEventKind.AGENT_CHANGED,
            payload={"agent_id": str(agent_id)},
        )


# ── Drain lifecycle Pydantic result + audit models ─────────────────────────


class ShutdownResult(BaseModel):
    """Per-agent outcome of a bulk ``shutdown_agents`` call."""

    agent_id: UUID
    outcome: Literal["draining", "already_draining", "already_shutdown", "not_found"]


class CancelShutdownResult(BaseModel):
    """Per-agent outcome of a bulk ``cancel_shutdown_agents`` call."""

    agent_id: UUID
    outcome: Literal["active", "not_draining", "already_shutdown", "not_found"]


class _AgentShutdownRequestedAudit(BaseModel):
    previous_lifecycle: str


class _AgentCancelShutdownRequestedAudit(BaseModel):
    previous_lifecycle: str


class _AgentShutdownCompleteAudit(BaseModel):
    pass


# ── Drain lifecycle service functions ───────────────────────────────────────


async def mark_agent_shutdown_complete(
    *,
    agent_id: UUID,
    session: AsyncSession,
) -> bool:
    """Atomic CAS ``lifecycle='shutdown' WHERE id=? AND lifecycle='draining'``.

    Executes all four side effects inside the caller's transaction:
    (1) CAS UPDATE; (2) revoke all active bearers; (3) write
    ``workspace_agent.shutdown_complete`` audit; (4) publish ``agent_changed``
    SSE-after-commit.

    Returns ``True`` when this caller wins the CAS; ``False`` when the row was
    already past ``draining`` (another pod beat this one — no side effects).
    Propagates bearer-revoke exceptions so the caller's transaction rolls back,
    keeping the CAS consistent.

    Caller commits.
    """
    from app.core.agent_gateway import bearers as _bearers  # noqa: PLC0415
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415
    from app.core.audit_log import Actor, ActorKind, audit  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    # 1. CAS UPDATE.
    result = await session.execute(
        update(WorkspaceAgentRow)
        .where(
            WorkspaceAgentRow.id == agent_id,
            WorkspaceAgentRow.lifecycle == "draining",
        )
        .values(lifecycle="shutdown")
        .returning(WorkspaceAgentRow.org_id)
    )
    org_id_val = result.scalar_one_or_none()
    if org_id_val is None:
        return False

    # 2. Revoke all active bearers (propagate on failure — rolls back the CAS).
    await _bearers.revoke_all_for_agent(agent_id, "shutdown_complete", session=session)

    # 3. Audit.
    await audit(
        "workspace_agent",
        agent_id,
        "workspace_agent.shutdown_complete",
        _AgentShutdownCompleteAudit(),
        Actor(kind=ActorKind.SYSTEM),
        org_id=org_id_val,
        session=session,
    )

    # 4. SSE — cache-invalidate so the SPA card flips lifecycle.
    publish_general_after_commit(
        session,
        org_id=org_id_val,
        kind=GeneralEventKind.AGENT_CHANGED,
        payload={"agent_id": str(agent_id)},
    )

    return True


async def shutdown_agents(
    *,
    org_id: UUID,
    agent_ids: Sequence[UUID],
    actor: Actor,
    session: AsyncSession,
) -> list[ShutdownResult]:
    """Bulk request to transition agents to ``lifecycle='draining'``.

    Per-agent loop: ``SELECT … FOR UPDATE`` by ``(id, org_id)``; dispatches on
    current lifecycle; CAS UPDATE on win.  Never raises — per-row outcomes are
    surfaced via ``ShutdownResult.outcome``.  Caller commits.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415
    from app.core.audit_log import audit  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    results: list[ShutdownResult] = []

    for aid in agent_ids:
        row = (
            await session.execute(
                select(WorkspaceAgentRow)
                .where(
                    WorkspaceAgentRow.id == aid,
                    WorkspaceAgentRow.org_id == org_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if row is None:
            results.append(ShutdownResult(agent_id=aid, outcome="not_found"))
            continue

        lc = row.lifecycle
        if lc == "shutdown":
            results.append(ShutdownResult(agent_id=aid, outcome="already_shutdown"))
            continue
        if lc == "draining":
            results.append(ShutdownResult(agent_id=aid, outcome="already_draining"))
            continue

        # CAS: unconfigured | active → draining
        prev_lc = row.lifecycle
        cas = await session.execute(
            update(WorkspaceAgentRow)
            .where(
                WorkspaceAgentRow.id == aid,
                WorkspaceAgentRow.lifecycle.in_(["unconfigured", "active"]),
            )
            .values(lifecycle="draining")
            .returning(WorkspaceAgentRow.org_id)
        )
        if cas.scalar_one_or_none() is None:
            # Lost the race (another pod moved it between our SELECT and UPDATE).
            results.append(ShutdownResult(agent_id=aid, outcome="already_draining"))
            continue

        # Enqueue a ShutdownCommand so the agent sees the drain request on its
        # next claim. Pre-stamp agent_id so the priority-bucket SELECT finds it
        # without a workspace_ids sweep (same pattern as ConfigUpdate).
        from uuid import uuid7  # noqa: PLC0415

        from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

        shutdown_cmd = ShutdownCommand(command_id=uuid7(), traceparent="")
        await enqueue_command(org_id=org_id, command=shutdown_cmd, session=session)
        placeholder_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
        await session.execute(
            update(AgentCommandRow)
            .where(AgentCommandRow.id == shutdown_cmd.command_id)
            .values(agent_id=aid, completion_token_hash=placeholder_hash)
        )
        await session.flush()

        await audit(
            "workspace_agent",
            aid,
            "workspace_agent.shutdown_requested",
            _AgentShutdownRequestedAudit(previous_lifecycle=prev_lc),
            actor,
            org_id=org_id,
            session=session,
        )
        publish_general_after_commit(
            session,
            org_id=org_id,
            kind=GeneralEventKind.AGENT_CHANGED,
            payload={"agent_id": str(aid)},
        )
        results.append(ShutdownResult(agent_id=aid, outcome="draining"))

    return results


async def cancel_shutdown_agents(
    *,
    org_id: UUID,
    agent_ids: Sequence[UUID],
    actor: Actor,
    session: AsyncSession,
) -> list[CancelShutdownResult]:
    """Bulk request to transition agents from ``lifecycle='draining'`` back to ``active``.

    Symmetric to ``shutdown_agents`` — never raises, per-row outcomes.  Caller commits.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415
    from app.core.audit_log import audit  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    results: list[CancelShutdownResult] = []

    for aid in agent_ids:
        row = (
            await session.execute(
                select(WorkspaceAgentRow)
                .where(
                    WorkspaceAgentRow.id == aid,
                    WorkspaceAgentRow.org_id == org_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if row is None:
            results.append(CancelShutdownResult(agent_id=aid, outcome="not_found"))
            continue

        lc = row.lifecycle
        if lc == "shutdown":
            results.append(CancelShutdownResult(agent_id=aid, outcome="already_shutdown"))
            continue
        if lc in ("unconfigured", "active"):
            results.append(CancelShutdownResult(agent_id=aid, outcome="not_draining"))
            continue

        # CAS: draining → active
        prev_lc = row.lifecycle
        cas = await session.execute(
            update(WorkspaceAgentRow)
            .where(
                WorkspaceAgentRow.id == aid,
                WorkspaceAgentRow.lifecycle == "draining",
            )
            .values(lifecycle="active")
            .returning(WorkspaceAgentRow.org_id)
        )
        if cas.scalar_one_or_none() is None:
            # Lost the race.
            results.append(CancelShutdownResult(agent_id=aid, outcome="not_draining"))
            continue

        # Enqueue a CancelShutdownCommand so the agent sees the resume signal on
        # its next claim. Pre-stamp agent_id so the priority-bucket SELECT finds
        # it (same pattern as ConfigUpdate / ShutdownCommand).
        from uuid import uuid7  # noqa: PLC0415

        from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

        cancel_cmd = CancelShutdownCommand(command_id=uuid7(), traceparent="")
        await enqueue_command(org_id=org_id, command=cancel_cmd, session=session)
        placeholder_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
        await session.execute(
            update(AgentCommandRow)
            .where(AgentCommandRow.id == cancel_cmd.command_id)
            .values(agent_id=aid, completion_token_hash=placeholder_hash)
        )
        await session.flush()

        await audit(
            "workspace_agent",
            aid,
            "workspace_agent.cancel_shutdown_requested",
            _AgentCancelShutdownRequestedAudit(previous_lifecycle=prev_lc),
            actor,
            org_id=org_id,
            session=session,
        )
        publish_general_after_commit(
            session,
            org_id=org_id,
            kind=GeneralEventKind.AGENT_CHANGED,
            payload={"agent_id": str(aid)},
        )
        results.append(CancelShutdownResult(agent_id=aid, outcome="active"))

    return results


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
    Emits one ``agent_changed`` SSE event per transitioned agent via
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
                select(WorkspaceAgentRow)
                .where(
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                )
                .with_for_update(skip_locked=True)
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
            # Stuck-drain recovery: if the agent crashed mid-drain (lifecycle is
            # still "draining" but it stopped heartbeating), settle the lifecycle
            # to "shutdown" atomically in this same sweep tick.  The CAS inside
            # mark_agent_shutdown_complete is a no-op for non-draining rows, so
            # this call is safe for ALL newly-offline rows.
            await mark_agent_shutdown_complete(agent_id=row.id, session=session)

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
            kind=GeneralEventKind.AGENT_CHANGED,
            payload={"agent_id": str(row.id)},
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
            "lifecycle": row.lifecycle,
            "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
            "os": row.os,
            "cpu_count": row.cpu_count,
            "memory_bytes": row.memory_bytes,
            "claimed_workspace_count": row.claimed_workspace_count,
            "version": row.version,
        }
        for row in rows
    ]
