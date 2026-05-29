"""Per-agent in-memory dispatch queue + event ingestion + stale-claim guard.

The control plane keeps one FIFO per `agent_id`. Workflows write commands
into the queue via `enqueue_command(agent_id, command)`; the agent's
long-poll consumes them with `claim_next(agent_id, *, wait_seconds)`.
The queue is process-local — single-instance backends only at the POC scale.

Event ingestion (`record_agent_event`) delegates the stale-claim guard lookup
to the registered `WorkspaceAgentReportSink` (owned by `core/workspace`), then
enqueues `core/workflow.handle_agent_event` via the outbox in the same
transaction when the event is terminal.

The active `AgentQueues` instance is ContextVar-bound. `bind_agent_queues` is
the production DI seam — the composition root calls it at startup; the
`agent_queues_isolation` fixture in `app/testing/isolation` binds a fresh
instance per test. `get_agent_queues()` raises `RuntimeError` if called before
any bind — fail-fast so forgotten startup binds surface immediately.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway.report_sink import (
    WorkspaceEventReport,
    get_report_sink,
)
from app.core.agent_gateway.types import (
    AgentCommand,
    AgentEvent,
    AgentRef,
    HeartbeatRequest,
    HeartbeatResponse,
    StaleClaimError,
    WorkspaceEvent,
)
from app.core.tasks import enqueue

log = structlog.get_logger("core.agent_gateway")


# ── In-memory dispatch queues ───────────────────────────────────────────


@dataclass
class AgentQueues:
    """Per-process in-memory FIFO registry for agent dispatch.

    Holds one queue and one asyncio.Condition per agent_id. ContextVar-bound
    so each test context gets a fresh, isolated instance.
    """

    queues: dict[UUID, deque[AgentCommand]] = field(default_factory=lambda: defaultdict(deque))
    conditions: dict[UUID, asyncio.Condition] = field(default_factory=dict)
    conditions_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_agent_queues_var: ContextVar[AgentQueues | None] = ContextVar("_agent_queues_var", default=None)


def bind_agent_queues(instance: AgentQueues) -> None:
    """Bind `instance` as the active agent-queues registry for the current Context.

    Called once at process startup (composition root) and once per test
    (isolation fixture). Subsequent calls in the same Context replace the
    prior binding.
    """
    _agent_queues_var.set(instance)


def get_agent_queues() -> AgentQueues:
    """Return the active agent-queues registry. Raises `RuntimeError` if
    `bind_agent_queues` has not been called — fail-fast so forgotten startup
    binds surface immediately rather than silently producing wrong state."""
    instance = _agent_queues_var.get()
    if instance is None:
        raise RuntimeError(
            "agent queues not bound: call bind_agent_queues(AgentQueues()) at "
            "process startup or use the agent_queues_isolation fixture in tests."
        )
    return instance


async def _get_condition(agent_id: UUID) -> asyncio.Condition:
    registry = get_agent_queues()
    async with registry.conditions_lock:
        cond = registry.conditions.get(agent_id)
        if cond is None:
            cond = asyncio.Condition()
            registry.conditions[agent_id] = cond
        return cond


def queue_depth(agent_id: UUID) -> int:
    """Number of commands pending for `agent_id`."""
    return len(get_agent_queues().queues.get(agent_id, ()))


# ── Dispatch ────────────────────────────────────────────────────────────


async def enqueue_command(agent_id: UUID, command: AgentCommand) -> None:
    """Push an AgentCommand onto the agent's FIFO and wake any blocked
    long-poller. Called by `RemoteAgentWorkspaceProvider` from inside the
    workflow engine's start_step transaction."""
    get_agent_queues().queues[agent_id].append(command)
    cond = await _get_condition(agent_id)
    async with cond:
        cond.notify()


async def claim_next(
    agent_id: UUID,
    *,
    wait_seconds: int,
) -> AgentCommand | None:
    """Pop the head of the queue, or wait up to `wait_seconds` for one to
    arrive. Returns None on timeout (the agent then re-arms the poll).

    `wait_seconds=0` is a non-blocking peek — useful in tests."""
    queue = get_agent_queues().queues[agent_id]
    if queue:
        return queue.popleft()
    if wait_seconds <= 0:
        return None
    cond = await _get_condition(agent_id)
    async with cond:
        try:
            await asyncio.wait_for(
                cond.wait_for(lambda: bool(queue)),
                timeout=wait_seconds,
            )
        except TimeoutError:
            return None
    # Re-check under the lock-free dequeue — another waiter may have
    # popped it. If so, fall through to None.
    if queue:
        return queue.popleft()
    return None


# ── Heartbeat / reconciliation ─────────────────────────────────────────


async def record_heartbeat(
    agent_id: UUID,
    request: HeartbeatRequest,
    *,
    session: AsyncSession,
) -> HeartbeatResponse:
    """Bump `workspace_agents.last_heartbeat_at` for the pod identified
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
    else:
        # Heartbeat arrived for a pod the control plane doesn't know about —
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

    sink = get_report_sink()
    forgotten_ids = await sink.reconcile_heartbeat(reported_ids, session)

    return HeartbeatResponse(
        reconciled_at=datetime.now(UTC),
        forgotten_workspaces=tuple(forgotten_ids),
    )


# ── Event ingestion ────────────────────────────────────────────────────


async def record_agent_event(
    event: AgentEvent,
    *,
    session: AsyncSession,
) -> None:
    """Apply the stale-claim guard against the workspace, then — if the
    event is terminal — enqueue `workflow.handle_agent_event` via the
    outbox in the same transaction.

    Raises `StaleClaimError` when the workspace's `current_command_id`
    no longer matches; the endpoint maps this to `410 Gone`.

    Required `session`; caller commits.
    """
    # Look up the workspace holding this command. The single-flight claim
    # writes `current_command_id` + `current_holder_workflow_id` on the
    # workspace; delegates to the registered sink so workspace-state access
    # stays inside core/workspace.
    sink = get_report_sink()
    holder_workflow_id = await sink.resolve_claim(event.command_id, session)
    if holder_workflow_id is None:
        raise StaleClaimError(f"no workspace holds command {event.command_id}")

    if not event.is_terminal():
        # Non-terminal events (progress) skip workflow-engine resumption —
        # only `completed_*` events resume the workflow state machine.
        # Republish to the org-scoped workspace-activity channel so the SPA's
        # SSE live-tail picks them up. The WebSocket batch handler and this
        # HTTP path both write to the same `publish_workspace_activity` surface
        # so the SPA subscriber sees events regardless of the transport.
        log.info(
            "agent.event.progress",
            command_id=str(event.command_id),
        )
        from app.core.auth import require_org_context  # noqa: PLC0415
        from app.core.sse import publish_workspace_activity  # noqa: PLC0415

        await publish_workspace_activity(
            org_id=require_org_context(),
            workflow_execution_id=holder_workflow_id,
            payload=event.model_dump(mode="json"),
        )
        return

    # Terminal — enqueue the workflow handler. The outbox row goes in the
    # caller's session so the workflow advance is atomic with whatever the
    # endpoint commits.
    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": str(holder_workflow_id),
            "agent_command_id": str(event.command_id),
            "outcome_label": event.outcome_label or "success",
            "outputs": dict(event.outputs),
            "traceparent": event.traceparent,
        },
        session=session,
    )


async def record_workspace_event(
    event: WorkspaceEvent,
    *,
    session: AsyncSession,
) -> None:
    """Update the workspace mirror from an agent-reported state change.

    Delegates all workspace-state access to the registered sink. The sink
    applies the stale-claim guard and the kind→status map, returning an
    outcome VO. agent_gateway maps `accepted=False` to `StaleClaimError`
    so the endpoint can return `410 Gone`.
    """
    sink = get_report_sink()
    report = WorkspaceEventReport(
        workspace_id=event.workspace_id,
        command_id=event.command_id,
        kind=event.kind,
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
    agent_pod_id: UUID,
    iam_arn: str,
    version: str | None,
    session: AsyncSession,
) -> UUID:
    """Insert or update the `workspace_agents` row for `(org_id, agent_pod_id)`
    on a successful identity exchange. Returns the row's `id` — this is
    the `agent_id` the bearer is scoped to and that subsequent endpoints
    use to address the pod. Caller commits."""
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    row = (
        await session.execute(
            select(WorkspaceAgentRow).where(
                WorkspaceAgentRow.org_id == org_id,
                WorkspaceAgentRow.agent_pod_id == agent_pod_id,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = WorkspaceAgentRow(
            org_id=org_id,
            agent_pod_id=agent_pod_id,
            iam_arn=iam_arn,
            version=version,
            last_heartbeat_at=now,
            state="reachable",
        )
        session.add(row)
        await session.flush()
    else:
        row.iam_arn = iam_arn
        row.version = version
        row.last_heartbeat_at = now
        row.state = "reachable"
    return row.id


async def get_agent_info(
    agent_id: UUID,
    *,
    session: AsyncSession,
) -> dict | None:
    """Return a plain dict snapshot of the agent row, or None if absent.

    Keys: `id`, `org_id`, `agent_pod_id`, `iam_arn`, `version`, `state`,
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
        "agent_pod_id": row.agent_pod_id,
        "iam_arn": row.iam_arn,
        "version": row.version,
        "state": row.state,
        "last_heartbeat_at": row.last_heartbeat_at,
    }


async def pick_agent_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> AgentRef | None:
    """Pick the least-loaded reachable agent for `org_id`.

    Selects reachable pods (heartbeat within 90 s) and returns the one with the
    smallest in-process queue depth; ties break on most-recent heartbeat so a
    fresh pod beats a stale one when both are idle.

    Returns `None` when no reachable pod exists for the org.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(seconds=90)
    rows = (
        (
            await session.execute(
                select(WorkspaceAgentRow)
                .where(
                    WorkspaceAgentRow.org_id == org_id,
                    WorkspaceAgentRow.state == "reachable",
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                    WorkspaceAgentRow.last_heartbeat_at >= cutoff,
                )
                .order_by(WorkspaceAgentRow.last_heartbeat_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    best = min(
        rows,
        key=lambda r: (queue_depth(r.id), -(r.last_heartbeat_at.timestamp() if r.last_heartbeat_at else 0)),
    )
    return AgentRef(agent_id=best.id, agent_pod_id=best.agent_pod_id)


async def has_any_reachable_agent(
    *,
    session: AsyncSession,
) -> bool:
    """Return `True` when at least one workspace-agent pod heartbeated within
    the last 90 s — used by health-check callers to avoid cross-module Row
    access.
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

    - `connected` — at least one pod heartbeated within the last 90s
    - `lost` — at least one row exists but none recent enough
    - `not_configured` — no rows at all for this org
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


async def has_stale_agents_for_org(
    org_id: UUID,
    *,
    cutoff: datetime,
    session: AsyncSession,
) -> bool:
    """Return ``True`` when the org has no `workspace_agents` row whose
    `last_heartbeat_at` is at or after *cutoff*.

    A ``True`` result means every agent pod for the org is stale (or no
    pods have ever registered). Used by `core/workspace` to identify orgs
    that have lost their agent fleet without importing `workspace_agents`
    directly.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(WorkspaceAgentRow.id)
                .where(
                    WorkspaceAgentRow.org_id == org_id,
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                    WorkspaceAgentRow.last_heartbeat_at >= cutoff,
                )
                .limit(1)
            )
        )
        .tuples()
        .all()
    )
    return not bool(rows)
