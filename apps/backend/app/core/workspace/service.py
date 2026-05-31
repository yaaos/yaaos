"""Lifecycle service + registry + reaper for `core/workspace`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, ActorKind, audit_for_workspace
from app.core.database import session as get_session
from app.core.observability import spawn
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import (
    CodingAgentCliResult,
    HealthStatus,
    OnStreamLine,
    Workspace,
    WorkspaceClaimState,
    WorkspaceCommandState,
    WorkspaceError,
    WorkspaceInfo,
    WorkspaceNotFoundError,
    WorkspaceProvider,
    WorkspaceProvisionError,
    WorkspaceSpec,
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
        if provider.meta.id in self._providers:
            raise ValueError(f"workspace provider {provider.meta.id!r} already registered")
        self._providers[provider.meta.id] = provider

    def replace(self, provider: WorkspaceProvider) -> None:
        """Overwrite-or-insert; used by stub helpers."""
        self._providers[provider.meta.id] = provider

    def get(self, provider_id: str) -> WorkspaceProvider:
        try:
            return self._providers[provider_id]
        except KeyError as e:
            raise WorkspaceError(f"workspace provider not found: {provider_id}") from e

    def get_or_none(self, provider_id: str) -> WorkspaceProvider | None:
        """Return the provider for `provider_id`, or None if not registered.
        Used in paths where an unknown provider id is a warning-level event,
        not an exception (e.g. get_workspace, _attempt_destroy)."""
        return self._providers.get(provider_id)

    def is_registered(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def list(self) -> list[WorkspaceProvider]:
        return list(self._providers.values())

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


class _WorkspaceImpl:
    """Concrete Workspace that satisfies the Protocol.

    Holds an opaque `plugin_state` privately (not exposed to consumers) and
    delegates `run_coding_agent_cli` to the provider that produced the state.
    """

    def __init__(self, id: str, provider: WorkspaceProvider, plugin_state: dict[str, Any]) -> None:
        self.id = id
        self._provider = provider
        self._plugin_state = plugin_state

    async def info(self) -> WorkspaceInfo:
        return await _read_info(UUID(self.id))

    async def run_coding_agent_cli(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
        on_stream_line: OnStreamLine | None = None,
    ) -> CodingAgentCliResult:
        return await self._provider.run_coding_agent_cli(
            self._plugin_state,
            argv,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            on_stream_line=on_stream_line,
        )

    async def read_text(self, path: str) -> str | None:
        return await self._provider.read_text(self._plugin_state, path)

    async def write_text(self, path: str, content: str) -> None:
        await self._provider.write_text(self._plugin_state, path, content)


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


async def create_workspace(
    provider_id: str,
    spec: WorkspaceSpec,
    *,
    org_id: UUID,
) -> Workspace:
    """Provision a workspace via the named provider. Row is created in 'creating';
    flipped to 'active' after the plugin's provision() returns.
    """
    provider = get_provider(provider_id)
    expires_at = _utcnow() + timedelta(seconds=spec.resource_caps.wallclock_seconds)

    # Stamp org_id onto the spec so the provider can request auth tokens for
    # the right org via vcs. The spec passed in may or may not already have it;
    # we always overwrite to keep the parameter authoritative.
    spec = spec.model_copy(update={"org_id": org_id})

    async with get_session() as s:
        row = WorkspaceRow(
            org_id=org_id,
            provider_id=provider_id,
            spec=spec.model_dump(mode="json"),
            status=WorkspaceStatus.CREATING.value,
            expires_at=expires_at,
        )
        s.add(row)
        await s.flush()
        ws_id = row.id
        await s.commit()

    try:
        plugin_state = await provider.provision(spec)
    except Exception as e:
        async with get_session() as s:
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == ws_id)
                .values(
                    status=WorkspaceStatus.DESTROY_FAILED.value,
                    last_destroy_error=f"provision failed: {e}",
                )
            )
            await _audit_transition(
                s,
                workspace_id=ws_id,
                org_id=org_id,
                from_state=WorkspaceStatus.CREATING.value,
                to_state=WorkspaceStatus.DESTROY_FAILED.value,
                reason="provision_failed",
                error=str(e),
            )
            await s.commit()
        raise WorkspaceProvisionError(str(e)) from e

    async with get_session() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(WorkspaceRow.id == ws_id)
            .values(
                status=WorkspaceStatus.ACTIVE.value,
                activated_at=_utcnow(),
                plugin_state=plugin_state,
            )
        )
        await _audit_transition(
            s,
            workspace_id=ws_id,
            org_id=org_id,
            from_state=WorkspaceStatus.CREATING.value,
            to_state=WorkspaceStatus.ACTIVE.value,
            reason="provisioned",
        )
        await s.commit()

    log.info("workspace.created", workspace_id=str(ws_id), provider_id=provider_id)
    return _WorkspaceImpl(id=str(ws_id), provider=provider, plugin_state=plugin_state)


async def close_workspace(workspace_id: UUID) -> None:
    """Mark the workspace expired so the reaper picks it up. Idempotent."""
    async with get_session() as s:
        row = (
            await s.execute(
                select(WorkspaceRow).where(
                    WorkspaceRow.id == workspace_id,
                    WorkspaceRow.status.in_([WorkspaceStatus.ACTIVE.value, WorkspaceStatus.CREATING.value]),
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


@asynccontextmanager
async def with_workspace(
    provider_id: str,
    spec: WorkspaceSpec,
    *,
    org_id: UUID,
):
    """Context manager that provisions then closes (flips to expired) on exit."""
    ws = await create_workspace(provider_id, spec, org_id=org_id)
    try:
        yield ws
    finally:
        try:
            await close_workspace(UUID(ws.id))
        except Exception:
            log.exception("workspace.close_failed", workspace_id=ws.id)


async def get_workspace_info(workspace_id: UUID) -> WorkspaceInfo:
    return await _read_info(workspace_id)


async def get_workspace(workspace_id: UUID) -> Workspace | None:
    """Load a live `Workspace` handle for `workspace_id`, or None if the
    row is missing / not active. Substrate for Workspace WorkflowCommand
    bodies that take a `workspace_id` input (e.g. CodeReview) and need to
    run a coding-agent CLI against the existing workspace.

    Returns None when:
    - the row doesn't exist
    - the row's `plugin_state` is unset (workspace failed to provision)
    - the row's provider isn't registered (deployment-level misconfig —
      caller surfaces this as a workflow failure)
    """
    async with get_session() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one_or_none()
    if row is None or row.plugin_state is None:
        return None
    provider = current_workspace_registry().get_or_none(row.provider_id)
    if provider is None:
        log.warning(
            "workspace.get_workspace.provider_not_registered",
            workspace_id=str(workspace_id),
            provider_id=row.provider_id,
        )
        return None
    return _WorkspaceImpl(id=str(row.id), provider=provider, plugin_state=row.plugin_state)


async def _seed_workspace_for_tests(
    *,
    org_id: UUID,
    provider_id: str,
    plugin_state: dict,
    sha: str,
    current_command_id: UUID | None = None,
    current_holder_workflow_id: UUID | None = None,
    status: str | None = None,
    caller_session: AsyncSession | None = None,
) -> str:
    """Insert a workspace row in `active` state with caller-supplied plugin_state.

    For cross-module tests that need a workspace in the DB without going through
    the full provision flow. Returns the workspace id string.

    When `caller_session` is supplied the row is added to the caller's transaction
    (no commit — the caller commits). When omitted a new session is opened and
    committed immediately.

    `current_command_id` and `current_holder_workflow_id` are optional — set
    them when the test needs to simulate a claimed workspace (agent_gateway tests).
    """
    from datetime import timedelta  # noqa: PLC0415

    def _build_row() -> WorkspaceRow:
        return WorkspaceRow(
            org_id=org_id,
            provider_id=provider_id,
            spec={"sha": sha},
            status=status or WorkspaceStatus.ACTIVE.value,
            expires_at=_utcnow() + timedelta(hours=1),
            plugin_state=plugin_state,
            current_command_id=current_command_id,
            current_holder_workflow_id=current_holder_workflow_id,
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
    """Flip every active/creating workspace for the org to expired. Returns count.

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
                        WorkspaceRow.status.in_(
                            [WorkspaceStatus.ACTIVE.value, WorkspaceStatus.CREATING.value]
                        ),
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
            await _failsafe_agent_loss(s, set(newly_offline))

        # 1d. Command-lease reaper — requeue claimed commands whose 30-second
        # receipt deadline has passed without a `received` event from the agent.
        from app.core.agent_gateway import requeue_stale_claimed  # noqa: PLC0415

        await requeue_stale_claimed(session=s)

        # 2. Find rows to destroy.
        rows = (
            (
                await s.execute(
                    select(WorkspaceRow)
                    .where(
                        WorkspaceRow.status.in_(
                            [WorkspaceStatus.EXPIRED.value, WorkspaceStatus.CREATING.value]
                        ),
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


async def _failsafe_agent_loss(s: Any, offline_agent_ids: set[UUID]) -> None:
    """Mark workspaces EXPIRED + revoke their owning pod's bearers for each
    newly-offline agent (failsafe 6).

    Per-pod: only the supplied `offline_agent_ids` are processed — healthy
    sibling pods keep their bearers and their workspaces untouched.

    For each expired workspace that holds an in-flight `current_command_id`,
    a synthetic terminal-failure event is enqueued via `HANDLE_AGENT_EVENT`
    so the owning WorkflowExecution resumes (fails the step) rather than
    hanging in AWAITING_AGENT indefinitely.

    Workspaces with a NULL `owning_agent_id` (legacy rows) are skipped — they
    carry no owning pod to declare lost.

    Called both by the reaper sweep (with the newly-offline set from
    `compute_agent_liveness_transitions`) and eagerly by the graceful-shutdown
    DELETE handler (with the single agent's ID).
    """
    from app.core.agent_gateway import revoke_all_for_agent  # noqa: PLC0415
    from app.core.tasks import enqueue  # noqa: PLC0415
    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    # Active/Creating workspaces whose owning pod is in the offline set.
    candidate_rows = (
        await s.execute(
            select(
                WorkspaceRow.id,
                WorkspaceRow.org_id,
                WorkspaceRow.status,
                WorkspaceRow.owning_agent_id,
                WorkspaceRow.current_command_id,
                WorkspaceRow.current_holder_workflow_id,
            ).where(
                WorkspaceRow.owning_agent_id.in_(offline_agent_ids),
                WorkspaceRow.status.in_([WorkspaceStatus.ACTIVE.value, WorkspaceStatus.CREATING.value]),
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
    for ws_id, org_id, status, _owning_agent_id, current_command_id, holder_workflow_id in candidate_rows:
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
        # waiting forever in AWAITING_AGENT.
        if current_command_id is not None and holder_workflow_id is not None:
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
        await provider.destroy(row.plugin_state or {})
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


async def _reaper_loop(interval_seconds: int) -> None:
    while True:
        try:
            await _reaper_sweep_once()
        except Exception:
            log.exception("workspace.reaper_sweep_failed")
        await asyncio.sleep(interval_seconds)


async def startup_recovery() -> None:
    """Flip any non-terminal workspace from a prior process to 'expired'."""
    async with get_session() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(
                WorkspaceRow.status.in_(
                    [
                        WorkspaceStatus.CREATING.value,
                        WorkspaceStatus.ACTIVE.value,
                        WorkspaceStatus.DESTROYING.value,
                    ]
                )
            )
            .values(status=WorkspaceStatus.EXPIRED.value)
        )
        await s.commit()


def start_reaper(interval_seconds: int) -> None:
    """Spawn the reaper loop. Called from FastAPI's lifespan."""
    spawn("workspace.reaper", _reaper_loop(interval_seconds))


async def health_check_all() -> dict[str, HealthStatus]:
    """Aggregate health across registered providers (used by settings)."""
    out: dict[str, HealthStatus] = {}
    for plugin_id, provider in current_workspace_registry()._providers.items():
        try:
            out[plugin_id] = await provider.health_check()
        except Exception as e:
            out[plugin_id] = HealthStatus(healthy=False, message=str(e), checked_at=_utcnow())
    return out


async def get_workspace_claim_state(
    command_id: UUID,
    session: AsyncSession,
) -> WorkspaceClaimState | None:
    """Return the claim projection for the workspace holding `command_id`, or
    None if no workspace is currently claimed by that command.

    Used by `core/agent_gateway` to apply the stale-claim guard and locate the
    workflow-execution holder without crossing the module boundary via a raw Row.
    """
    row = (
        await session.execute(
            select(
                WorkspaceRow.id,
                WorkspaceRow.current_holder_workflow_id,
                WorkspaceRow.status,
                WorkspaceRow.owning_agent_id,
            ).where(WorkspaceRow.current_command_id == command_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkspaceClaimState(
        workspace_id=row[0],
        current_holder_workflow_id=row[1],
        status=row[2],
        owning_agent_id=row[3],
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
