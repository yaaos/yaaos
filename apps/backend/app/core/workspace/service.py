"""Lifecycle service + registry + reaper for `core/workspace`."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, ActorKind, audit_for_workspace
from app.core.database import session as get_session
from app.core.tasks import scheduled
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import (
    HealthStatus,
    WorkspaceClaimState,
    WorkspaceCommandState,
    WorkspaceError,
    WorkspaceInfo,
    WorkspaceNotFoundError,
    WorkspaceOwner,
    WorkspaceProvider,
    WorkspaceStatus,
)


class _WorkspaceTransitionAudit(BaseModel):
    """Audit payload for every `workspace.transitioned` row.

    `reason` discriminates the transition cause so the org-settings security
    feed can render meaningful one-liners ("Idle timeout", "Agent lost",
    "Manually closed"). System-driven transitions use `ActorKind.SYSTEM`;
    admin actions populate `actor_user_id`.
    """

    from_state: str
    to_state: str
    reason: str
    error: str | None = None


_SYSTEM_ACTOR = Actor(kind=ActorKind.SYSTEM)


async def _audit_transition(
    s: Any,
    *,
    workspace_id: UUID,
    org_id: UUID,
    from_state: str,
    to_state: str,
    reason: str,
    error: str | None = None,
    actor: Actor | None = None,
) -> None:
    """Write a `workspace.transitioned` audit row. Failsafe-7 coverage —
    every state change in this module routes through here."""
    await audit_for_workspace(
        workspace_id,
        "workspace.transitioned",
        _WorkspaceTransitionAudit(from_state=from_state, to_state=to_state, reason=reason, error=error),
        actor=actor or _SYSTEM_ACTOR,
        org_id=org_id,
        session=s,
    )


log = structlog.get_logger("workspace")


class WorkspaceRegistry:
    """Workspace provider map. ContextVar-bound so each test context gets a
    fresh, isolated instance; production rides the import-time default for the
    process lifetime — it never calls bind_workspace_registry(). The ContextVar
    exists solely for per-test isolation (see app/testing/isolation.py)."""

    def __init__(self) -> None:
        self._providers: dict[str, WorkspaceProvider] = {}

    def register(self, provider: WorkspaceProvider) -> None:
        if provider.plugin_id in self._providers:
            raise ValueError(f"workspace provider {provider.plugin_id!r} already registered")
        self._providers[provider.plugin_id] = provider

    def replace(self, provider: WorkspaceProvider) -> None:
        """Overwrite-or-insert; used by stub helpers."""
        self._providers[provider.plugin_id] = provider

    def get(self, provider_id: str) -> WorkspaceProvider:
        try:
            return self._providers[provider_id]
        except KeyError as e:
            raise WorkspaceError(f"workspace provider not found: {provider_id}") from e

    def get_or_none(self, provider_id: str) -> WorkspaceProvider | None:
        """Return the provider for `provider_id`, or None if not registered.
        Used in paths where an unknown provider id is a warning-level event,
        not an exception (e.g. _attempt_destroy)."""
        return self._providers.get(provider_id)

    def is_registered(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def list(self) -> list[WorkspaceProvider]:
        return list(self._providers.values())

    def items(self) -> tuple[tuple[str, WorkspaceProvider], ...]:
        """Return a snapshot of (provider_id, provider) pairs.

        Returns a tuple so callers cannot mutate registry state through the
        returned collection.
        """
        return tuple(self._providers.items())

    def copy(self) -> WorkspaceRegistry:
        clone = WorkspaceRegistry()
        clone._providers = dict(self._providers)
        return clone


_providers_var: ContextVar[WorkspaceRegistry | None] = ContextVar("_workspace_registry_var", default=None)
# Import-time default: plugins that call register_workspace_provider() at
# module-import time (bootstrap()) land here when no per-test binding is active.
# Production never calls bind_workspace_registry(); the ContextVar exists solely
# for per-test isolation.
_default_registry = WorkspaceRegistry()


def bind_workspace_registry(instance: WorkspaceRegistry) -> None:
    _providers_var.set(instance)


def current_workspace_registry() -> WorkspaceRegistry:
    return _providers_var.get() or _default_registry


def register_workspace_provider(provider: WorkspaceProvider) -> None:
    current_workspace_registry().register(provider)


def get_provider(provider_id: str) -> WorkspaceProvider:
    return current_workspace_registry().get(provider_id)


def is_workspace_provider_registered(plugin_id: str) -> bool:
    return current_workspace_registry().is_registered(plugin_id)


def list_workspace_providers() -> list[WorkspaceProvider]:
    """Return registered providers in insertion order."""
    return current_workspace_registry().list()


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _read_info(workspace_id: UUID) -> WorkspaceInfo:
    async with get_session() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one_or_none()
        if row is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        return _row_to_info(row)


def _row_to_info(row: WorkspaceRow) -> WorkspaceInfo:
    return WorkspaceInfo(
        id=str(row.id),
        provider_id=row.provider_id,
        sha=row.spec.get("sha", ""),
        status=WorkspaceStatus(row.status),
        created_at=row.created_at,
        activated_at=row.activated_at,
        expires_at=row.expires_at,
        destroyed_at=row.destroyed_at,
        age_seconds=(_utcnow() - row.created_at).total_seconds(),
    )


async def close_workspace(workspace_id: UUID) -> None:
    """Mark the workspace expired so the reaper picks it up. Idempotent."""
    async with get_session() as s:
        row = (
            await s.execute(
                select(WorkspaceRow).where(
                    WorkspaceRow.id == workspace_id,
                    WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        from_state = row.status
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == workspace_id)
            .values(status=WorkspaceStatus.EXPIRED.value)
        )
        await _audit_transition(
            s,
            workspace_id=workspace_id,
            org_id=row.org_id,
            from_state=from_state,
            to_state=WorkspaceStatus.EXPIRED.value,
            reason="closed",
        )
        await s.commit()
    log.info("workspace.closed", workspace_id=str(workspace_id))


async def get_workspace_info(workspace_id: UUID) -> WorkspaceInfo:
    return await _read_info(workspace_id)


async def _seed_workspace_for_tests(
    *,
    org_id: UUID,
    provider_id: str,
    sha: str,
    current_command_id: UUID | None = None,
    owning_agent_id: UUID | None = None,
    status: str | None = None,
    caller_session: AsyncSession | None = None,
) -> str:
    """Insert a workspace row in `active` state for test purposes.

    For cross-module tests that need a workspace in the DB without going through
    the full provision flow. Returns the workspace id string.

    When `caller_session` is supplied the row is added to the caller's transaction
    (no commit — the caller commits). When omitted a new session is opened and
    committed immediately.

    `current_command_id` is optional — set it when the test needs to simulate
    a claimed workspace (agent_gateway tests).
    """
    from datetime import timedelta  # noqa: PLC0415

    def _build_row() -> WorkspaceRow:
        return WorkspaceRow(
            org_id=org_id,
            provider_id=provider_id,
            spec={"sha": sha},
            status=status or WorkspaceStatus.ACTIVE.value,
            expires_at=_utcnow() + timedelta(hours=1),
            current_command_id=current_command_id,
            owning_agent_id=owning_agent_id,
        )

    if caller_session is not None:
        row = _build_row()
        caller_session.add(row)
        await caller_session.flush()
        return str(row.id)

    async with get_session() as s:
        row = _build_row()
        s.add(row)
        await s.flush()
        ws_id = row.id
        await s.commit()
    return str(ws_id)


async def force_close_all(*, org_id: UUID, reason: str = "force_close_all") -> int:
    """Flip every active workspace for the org to expired. Returns count.

    Used by Org Settings Disconnect / mode-switch. `reason` propagates to
    the audit row (`disconnect`, `mode_switch`, `arn_change`) so the
    security feed can render meaningful one-liners.
    """
    async with get_session() as s:
        rows = (
            (
                await s.execute(
                    select(WorkspaceRow).where(
                        WorkspaceRow.org_id == org_id,
                        WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == row.id)
                .values(status=WorkspaceStatus.EXPIRED.value)
            )
            await _audit_transition(
                s,
                workspace_id=row.id,
                org_id=org_id,
                from_state=row.status,
                to_state=WorkspaceStatus.EXPIRED.value,
                reason=reason,
            )
        await s.commit()
        return len(rows)


# Failsafe 6 threshold: an agent with no heartbeat for this many seconds is
# considered lost. Matches the 90s reachability cutoff used elsewhere.
AGENT_LOSS_HEARTBEAT_THRESHOLD_SECONDS = 90


async def _reaper_sweep_once() -> None:
    """One reaper pass — expire over-budget (TTL), idle-timeout, agent-loss
    (failsafe 6), then destroy expired rows + mark stuck destroys failed.

    Each transition writes an audit row (failsafe 7) via
    `_audit_transition` — selecting affected rows first instead of doing a
    bulk UPDATE so we can audit per-id.
    """
    now = _utcnow()
    async with get_session() as s:
        # 1. TTL sweep — expire over-budget actives.
        ttl_rows = (
            (
                await s.execute(
                    select(WorkspaceRow).where(
                        WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
                        WorkspaceRow.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in ttl_rows:
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == row.id)
                .values(status=WorkspaceStatus.EXPIRED.value)
            )
            await _audit_transition(
                s,
                workspace_id=row.id,
                org_id=row.org_id,
                from_state=WorkspaceStatus.ACTIVE.value,
                to_state=WorkspaceStatus.EXPIRED.value,
                reason="ttl_expired",
            )

        # 1b. Idle sweep — active workspaces with no claim that have been
        # activated longer than `max_idle_seconds` are abandoned.
        idle_rows = (
            (
                await s.execute(
                    select(WorkspaceRow).where(
                        WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
                        WorkspaceRow.current_command_id.is_(None),
                        WorkspaceRow.activated_at.is_not(None),
                        func.extract("epoch", now - WorkspaceRow.activated_at)
                        > WorkspaceRow.max_idle_seconds,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in idle_rows:
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == row.id)
                .values(status=WorkspaceStatus.EXPIRED.value)
            )
            await _audit_transition(
                s,
                workspace_id=row.id,
                org_id=row.org_id,
                from_state=WorkspaceStatus.ACTIVE.value,
                to_state=WorkspaceStatus.EXPIRED.value,
                reason="idle_timeout",
            )

        # 1b-ii. Liveness sweeper — compute reachable/stale/offline transitions
        # for all workspace-agent rows. Lives in core/agent_gateway (which owns
        # workspace_agents); called here because this loop runs each reaper tick.
        # Returns newly-offline agent IDs to feed directly into failsafe-6.
        from app.core.agent_gateway import compute_agent_liveness_transitions  # noqa: PLC0415

        newly_offline = await compute_agent_liveness_transitions(now, session=s)

        # 1c. Agent-loss (failsafe 6) — per-pod. A workspace whose owning
        # agent just went offline is expired and that pod's bearers revoked,
        # even when sibling pods in the same org are healthy.
        if newly_offline:
            await failsafe_agent_loss(s, set(newly_offline))

        # 1d. Command-lease reaper — requeue claimed commands whose 30-second
        # receipt deadline has passed without a `received` event from the agent.
        from app.core.agent_gateway import requeue_stale_claimed  # noqa: PLC0415

        await requeue_stale_claimed(session=s)

        # 2. Find expired rows to destroy.
        rows = (
            (
                await s.execute(
                    select(WorkspaceRow)
                    .where(
                        WorkspaceRow.status == WorkspaceStatus.EXPIRED.value,
                        WorkspaceRow.destroy_attempts < 3,
                    )
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        await s.commit()

    for row in rows:
        await _attempt_destroy(row)


async def failsafe_agent_loss(s: Any, offline_agent_ids: set[UUID]) -> None:
    """Mark workspaces EXPIRED + revoke their owning pod's bearers for each
    newly-offline agent (failsafe 6).

    Per-pod: only the supplied `offline_agent_ids` are processed — healthy
    sibling pods keep their bearers and their workspaces untouched.

    For each expired workspace that holds an in-flight `current_command_id`,
    the owning workflow execution is resolved from `agent_commands.workflow_execution_id`
    (the durable correlation column). A synthetic terminal-failure event is enqueued
    via `HANDLE_AGENT_EVENT` so the WorkflowExecution resumes (fails the step)
    rather than hanging in AWAITING_AGENT indefinitely.

    Workspaces with a NULL `owning_agent_id` (legacy rows) are skipped — they
    carry no owning pod to declare lost.

    Called both by the reaper sweep (with the newly-offline set from
    `compute_agent_liveness_transitions`) and eagerly by the graceful-shutdown
    DELETE handler (with the single agent's ID).
    """
    from app.core.agent_gateway import (  # noqa: PLC0415
        get_command_workflow_execution_id,
        revoke_all_for_agent,
    )
    from app.core.tasks import enqueue  # noqa: PLC0415
    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    # Active workspaces whose owning pod is in the offline set.
    # CREATING is excluded: lean-created rows enter ACTIVE on first event;
    # no workspace row ever enters CREATING in the lean lifecycle.
    candidate_rows = (
        await s.execute(
            select(
                WorkspaceRow.id,
                WorkspaceRow.org_id,
                WorkspaceRow.status,
                WorkspaceRow.owning_agent_id,
                WorkspaceRow.current_command_id,
            ).where(
                WorkspaceRow.owning_agent_id.in_(offline_agent_ids),
                WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
            )
        )
    ).all()
    if not candidate_rows:
        # Still revoke bearers even if there are no workspace rows (agent
        # may have been idle with no workspaces claimed).
        for owning_agent_id in offline_agent_ids:
            await revoke_all_for_agent(owning_agent_id, "agent_loss", session=s)
        return

    expired_count = 0
    for ws_id, org_id, status, _owning_agent_id, current_command_id in candidate_rows:
        await s.execute(
            update(WorkspaceRow).where(WorkspaceRow.id == ws_id).values(status=WorkspaceStatus.EXPIRED.value)
        )
        await _audit_transition(
            s,
            workspace_id=ws_id,
            org_id=org_id,
            from_state=status,
            to_state=WorkspaceStatus.EXPIRED.value,
            reason="agent_loss",
        )
        expired_count += 1

        # Synthesize a terminal failure for any in-flight command so the
        # owning WorkflowExecution resumes (fails its step) rather than
        # waiting forever in AWAITING_AGENT. Correlation comes from
        # agent_commands.workflow_execution_id — no workspace-row column needed.
        if current_command_id is not None:
            holder_workflow_id = await get_command_workflow_execution_id(current_command_id, session=s)
            if holder_workflow_id is not None:
                await enqueue(
                    HANDLE_AGENT_EVENT,
                    args={
                        "workflow_execution_id": str(holder_workflow_id),
                        "agent_command_id": str(current_command_id),
                        "outcome_label": "failure",
                        "outputs": {},
                        "traceparent": None,
                    },
                    session=s,
                )

    # Revoke each offline pod's bearers — that pod re-exchanges when it returns.
    for owning_agent_id in offline_agent_ids:
        await revoke_all_for_agent(owning_agent_id, "agent_loss", session=s)

    log.warning(
        "workspace.failsafe_agent_loss",
        offline_agent_count=len(offline_agent_ids),
        expired_count=expired_count,
    )


async def _attempt_destroy(row: WorkspaceRow) -> None:
    provider = current_workspace_registry().get_or_none(row.provider_id)
    if provider is None:
        log.warning("workspace.destroy_no_provider", workspace_id=str(row.id), provider_id=row.provider_id)
        async with get_session() as s:
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == row.id)
                .values(
                    status=WorkspaceStatus.DESTROY_FAILED.value,
                    last_destroy_error=f"provider {row.provider_id} not registered",
                )
            )
            await _audit_transition(
                s,
                workspace_id=row.id,
                org_id=row.org_id,
                from_state=row.status,
                to_state=WorkspaceStatus.DESTROY_FAILED.value,
                reason="provider_not_registered",
                error=f"provider {row.provider_id} not registered",
            )
            await s.commit()
        return

    async with get_session() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == row.id)
            .values(
                status=WorkspaceStatus.DESTROYING.value,
                destroy_attempts=row.destroy_attempts + 1,
                last_destroy_attempt_at=_utcnow(),
            )
        )
        await _audit_transition(
            s,
            workspace_id=row.id,
            org_id=row.org_id,
            from_state=row.status,
            to_state=WorkspaceStatus.DESTROYING.value,
            reason="destroy_attempt",
        )
        await s.commit()

    try:
        await provider.destroy()
    except Exception as e:
        log.warning("workspace.destroy_failed", workspace_id=str(row.id), error=str(e))
        async with get_session() as s:
            attempts = row.destroy_attempts + 1
            new_status = (
                WorkspaceStatus.DESTROY_FAILED.value if attempts >= 3 else WorkspaceStatus.EXPIRED.value
            )
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == row.id)
                .values(status=new_status, last_destroy_error=str(e))
            )
            await _audit_transition(
                s,
                workspace_id=row.id,
                org_id=row.org_id,
                from_state=WorkspaceStatus.DESTROYING.value,
                to_state=new_status,
                reason="destroy_failed",
                error=str(e),
            )
            await s.commit()
        return

    async with get_session() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == row.id)
            .values(
                status=WorkspaceStatus.DESTROYED.value,
                destroyed_at=_utcnow(),
                last_destroy_error=None,
            )
        )
        await _audit_transition(
            s,
            workspace_id=row.id,
            org_id=row.org_id,
            from_state=WorkspaceStatus.DESTROYING.value,
            to_state=WorkspaceStatus.DESTROYED.value,
            reason="destroyed",
        )
        await s.commit()
    log.info("workspace.destroyed", workspace_id=str(row.id))


async def run_workspace_reaper() -> None:
    """Body of the per-minute `workspace_reaper` `@scheduled` task. Wraps
    `_reaper_sweep_once` with a single-pass error log so a transient DB
    or agent_gateway hiccup doesn't poison the broker retry path.
    Idempotent — every sweep step reads fresh state from the DB.

    Module-public so service tests can invoke the body directly without
    going through the broker dispatch path.
    """
    try:
        await _reaper_sweep_once()
    except Exception as exc:
        # inside-span failure: taskiq wraps scheduled task bodies in a span
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, str(exc))
        log.exception("workspace.reaper_sweep_failed")
        raise


async def startup_recovery() -> None:
    """Flip any non-terminal workspace from a prior process to 'expired'.

    Lean-created rows enter ACTIVE on the agent's first workspace event —
    no row ever enters CREATING in the current lifecycle, so CREATING is
    absent from the recovery list. ACTIVE and DESTROYING are the states
    a crashed process may leave behind.
    """
    async with get_session() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(
                WorkspaceRow.status.in_(
                    [
                        WorkspaceStatus.ACTIVE.value,
                        WorkspaceStatus.DESTROYING.value,
                    ]
                )
            )
            .values(status=WorkspaceStatus.EXPIRED.value)
        )
        await s.commit()


async def health_check_all() -> dict[str, HealthStatus]:
    """Aggregate health across registered providers (used by settings)."""
    out: dict[str, HealthStatus] = {}
    for plugin_id, provider in current_workspace_registry().items():
        try:
            out[plugin_id] = await provider.health_check()
        except Exception as e:
            out[plugin_id] = HealthStatus(healthy=False, message=str(e), checked_at=_utcnow())
    return out


async def get_workspace_owner(
    workspace_id: UUID,
    session: AsyncSession,
) -> WorkspaceOwner | None:
    """Return the `(org_id, owning_agent_id)` projection for `workspace_id`,
    or None if the row is missing.

    Used by Workspace WorkflowCommand `dispatch` bodies that need to enqueue
    an AgentCommand pinned to the workspace's owning agent without crossing
    the module boundary via a raw Row.
    """
    row = (
        await session.execute(
            select(WorkspaceRow.id, WorkspaceRow.org_id, WorkspaceRow.owning_agent_id).where(
                WorkspaceRow.id == workspace_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkspaceOwner(workspace_id=row[0], org_id=row[1], owning_agent_id=row[2])


async def get_workspace_claim_state(
    command_id: UUID,
    session: AsyncSession,
) -> WorkspaceClaimState | None:
    """Return the claim projection for the workspace holding `command_id`, or
    None if no workspace is currently claimed by that command.

    Used by `core/agent_gateway` to apply the stale-claim guard and locate the
    workspace owner without crossing the module boundary via a raw Row.
    Workflow-execution correlation lives on `agent_commands.workflow_execution_id`.
    """
    row = (
        await session.execute(
            select(
                WorkspaceRow.id,
                WorkspaceRow.status,
                WorkspaceRow.owning_agent_id,
            ).where(WorkspaceRow.current_command_id == command_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkspaceClaimState(
        workspace_id=row[0],
        status=row[1],
        owning_agent_id=row[2],
    )


async def get_workspace_command_state(
    workspace_id: UUID,
    session: AsyncSession,
) -> WorkspaceCommandState | None:
    """Return the command-ownership projection for `workspace_id`, or None if
    the row doesn't exist.

    Used by `core/agent_gateway` to validate event ownership before applying a
    status update — no raw Row crosses the module boundary.
    """
    row = (
        await session.execute(
            select(
                WorkspaceRow.id,
                WorkspaceRow.current_command_id,
                WorkspaceRow.status,
                WorkspaceRow.owning_agent_id,
            ).where(WorkspaceRow.id == workspace_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkspaceCommandState(
        workspace_id=row[0],
        current_command_id=row[1],
        status=row[2],
        owning_agent_id=row[3],
    )


async def update_workspace_status(
    workspace_id: UUID,
    new_status: str,
    session: AsyncSession,
) -> None:
    """Update the `status` column for `workspace_id`.

    Used by `core/agent_gateway` when applying agent-reported state changes
    (ready → active, destroyed → destroyed, failed → destroy_failed) without
    requiring a raw WorkspaceRow import.
    """
    await session.execute(
        update(WorkspaceRow).where(WorkspaceRow.id == workspace_id).values(status=new_status)
    )


async def get_workspace_statuses(
    workspace_ids: set[UUID],
    session: AsyncSession,
) -> dict[UUID, str]:
    """Return a `{id: status}` map for the given workspace ids.

    Used by `core/agent_gateway` heartbeat reconciliation to identify workspaces
    the control plane has dropped or marked `destroyed` — callers compare the
    result against what the agent reports.
    """
    if not workspace_ids:
        return {}
    rows = (
        await session.execute(
            select(WorkspaceRow.id, WorkspaceRow.status).where(WorkspaceRow.id.in_(workspace_ids))
        )
    ).all()
    return {row[0]: row[1] for row in rows}


# Per-minute reaper. Cluster-safe via `core/tasks` per-tick claim — only one
# worker enqueues the sweep each slot. Idempotent body; safe to redeliver.
workspace_reaper = scheduled(
    name="workspace_reaper",
    cron="* * * * *",
    queue="default",
    max_retries=1,
)(run_workspace_reaper)
