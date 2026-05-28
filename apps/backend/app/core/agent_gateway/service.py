"""Per-agent in-memory dispatch queue + event ingestion + stale-claim guard.

The control plane keeps one FIFO per `agent_id`. Workflows write commands
into the queue via `enqueue_command(agent_id, command)`; the agent's
long-poll consumes them with `claim_next(agent_id, *, wait_seconds)`.
The queue is process-local — single-instance backends only at the POC
scale. Persisting across restarts is .Event ingestion (`record_agent_event`) consults the workspace claim
columns set by `core/workspace.dispatch.try_claim` to apply the
stale-claim guard, then enqueues `core/workflow.handle_agent_event` via
the outbox in the same transaction.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.core.workspace import (
    get_workspace_claim_state,
    get_workspace_command_state,
    get_workspace_statuses,
    update_workspace_status,
)

log = structlog.get_logger("core.agent_gateway")


# ── In-memory dispatch queues ───────────────────────────────────────────


_queues: dict[UUID, deque[AgentCommand]] = defaultdict(deque)
# One condition per agent so a long-poll wakes immediately when a command
# is enqueued for that specific agent. Created lazily.
_conditions: dict[UUID, asyncio.Condition] = {}
_conditions_lock = asyncio.Lock()


async def _get_condition(agent_id: UUID) -> asyncio.Condition:
    async with _conditions_lock:
        cond = _conditions.get(agent_id)
        if cond is None:
            cond = asyncio.Condition()
            _conditions[agent_id] = cond
        return cond


def queue_depth(agent_id: UUID) -> int:
    """Test helper — number of commands pending for `agent_id`."""
    return len(_queues.get(agent_id, ()))


def clear_queues() -> None:
    """Drop every in-memory queue and condition."""
    _queues.clear()
    _conditions.clear()


# ── Dispatch ────────────────────────────────────────────────────────────


async def enqueue_command(agent_id: UUID, command: AgentCommand) -> None:
    """Push an AgentCommand onto the agent's FIFO and wake any blocked
    long-poller. Called by `RemoteAgentWorkspaceProvider` from inside the
    workflow engine's start_step transaction."""
    _queues[agent_id].append(command)
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
    queue = _queues[agent_id]
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
    # forget. The check looks up rows by id and identifies the deltas.
    reported_ids = {w.workspace_id for w in request.workspaces}
    if not reported_ids:
        return HeartbeatResponse(reconciled_at=datetime.now(UTC), forgotten_workspaces=())

    alive_or_known = await get_workspace_statuses(reported_ids, session)
    forgotten: list[UUID] = []
    for entry in request.workspaces:
        status = alive_or_known.get(entry.workspace_id)
        if status is None or status == "destroyed":
            forgotten.append(entry.workspace_id)

    return HeartbeatResponse(
        reconciled_at=datetime.now(UTC),
        forgotten_workspaces=tuple(forgotten),
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
    # workspace; the lookup chain is `event.command_id → workspaces →
    # current_holder_workflow_id → workflow_executions`.
    claim = await get_workspace_claim_state(event.command_id, session)
    if claim is None:
        raise StaleClaimError(f"no workspace holds command {event.command_id}")
    if claim.current_holder_workflow_id is None:
        # Defensive: a claim without a workflow holder shouldn't exist —
        # treat as stale so the agent abandons silently.
        raise StaleClaimError(f"workspace {claim.workspace_id} has no current_holder_workflow_id")

    if not event.is_terminal():
        # Non-terminal events (progress) skip workflow-engine resumption —
        # only `completed_*` events resume the workflow state machine.
        # Republish to the org-scoped workspace-activity channel so the SPA's
        # SSE live-tail picks them up. The WebSocket batch handler and this
        # HTTP path both write to the same `publish_workspace_activity` surface
        # so the SPA subscriber sees events regardless of the transport.
        log.info(
            "agent.event.progress",
            workspace_id=str(claim.workspace_id),
            command_id=str(event.command_id),
        )
        from app.core.auth import require_org_context  # noqa: PLC0415
        from app.core.sse import publish_workspace_activity  # noqa: PLC0415

        await publish_workspace_activity(
            org_id=require_org_context(),
            workflow_execution_id=claim.current_holder_workflow_id,
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
            "workflow_execution_id": str(claim.current_holder_workflow_id),
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
    Applies the same stale-claim guard as `record_agent_event`."""
    ws_cmd = await get_workspace_command_state(event.workspace_id, session)
    if ws_cmd is None:
        raise StaleClaimError(f"unknown workspace {event.workspace_id}")
    if ws_cmd.current_command_id != event.command_id and ws_cmd.current_command_id is not None:
        raise StaleClaimError(
            f"workspace {ws_cmd.workspace_id} command {ws_cmd.current_command_id} != event command {event.command_id}"
        )

    # Map agent-side workspace kind to control-plane status.
    new_status: str | None = None
    if event.kind == "ready":
        new_status = "active"
    elif event.kind == "destroyed":
        new_status = "destroyed"
    elif event.kind == "failed":
        new_status = "destroy_failed"

    if new_status is not None:
        await update_workspace_status(event.workspace_id, new_status, session)

    log.info(
        "agent.workspace_event",
        workspace_id=str(ws_cmd.workspace_id),
        kind=event.kind,
        new_status=new_status,
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
