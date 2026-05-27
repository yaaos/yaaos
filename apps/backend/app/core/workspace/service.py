"""Lifecycle service + registry + reaper for `core/workspace`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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


_PROVIDERS: dict[str, WorkspaceProvider] = {}


def register_workspace_provider(provider: WorkspaceProvider) -> None:
    if provider.meta.id in _PROVIDERS:
        raise ValueError(f"workspace provider {provider.meta.id!r} already registered")
    _PROVIDERS[provider.meta.id] = provider


def get_provider(provider_id: str) -> WorkspaceProvider:
    try:
        return _PROVIDERS[provider_id]
    except KeyError as e:
        raise WorkspaceError(f"workspace provider not found: {provider_id}") from e


def clear_workspace_providers() -> None:
    """Clear the workspace provider registry."""
    _PROVIDERS.clear()


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

    ws_id = uuid4()
    async with get_session() as s:
        row = WorkspaceRow(
            id=ws_id,
            org_id=org_id,
            provider_id=provider_id,
            spec=spec.model_dump(mode="json"),
            status=WorkspaceStatus.CREATING.value,
            expires_at=expires_at,
        )
        s.add(row)
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
    provider = _PROVIDERS.get(row.provider_id)
    if provider is None:
        log.warning(
            "workspace.get_workspace.provider_not_registered",
            workspace_id=str(workspace_id),
            provider_id=row.provider_id,
        )
        return None
    return _WorkspaceImpl(id=str(row.id), provider=provider, plugin_state=row.plugin_state)


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

        # 1c. Agent-loss (failsafe 6) — orgs running remote agents where
        # NO pod has heartbeated within the threshold have all their
        # workspaces expired and bearers revoked. POC approximation: we
        # match per-org (not per-pod) since WorkspaceRow has no agent_id.
        await _failsafe_agent_loss(s, now)

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


async def _failsafe_agent_loss(s: Any, now: datetime) -> None:
    """Mark workspaces EXPIRED + revoke bearers when their org's remote agents
    have all gone stale beyond the heartbeat threshold (failsafe 6).

    POC scope: per-org check rather than per-pod (workspaces don't carry
    `agent_id` directly). An org with remote_agent provider whose every
    `workspace_agents` row has a stale `last_heartbeat_at` (or none ever)
    is considered to have lost its agent fleet. Workspaces in non-terminal
    states transition to EXPIRED with reason `agent_loss`.

    Bearer revocation goes through `core.agent_gateway.bearers` (lazy
    import to keep the workspace module from importing the gateway at
    init time).
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    cutoff = now - timedelta(seconds=AGENT_LOSS_HEARTBEAT_THRESHOLD_SECONDS)
    # Find orgs in remote-agent mode where every workspace_agents row is
    # stale (or there are none at all but a workspace exists in flight).
    stale_orgs = (
        await s.execute(
            sa_text(
                """
                SELECT o.id
                  FROM orgs o
                 WHERE o.workspace_provider = 'remote_agent'
                   AND EXISTS (
                         SELECT 1 FROM workspaces w
                          WHERE w.org_id = o.id
                            AND w.status IN ('creating', 'active')
                   )
                   AND NOT EXISTS (
                         SELECT 1 FROM workspace_agents a
                          WHERE a.org_id = o.id
                            AND a.last_heartbeat_at IS NOT NULL
                            AND a.last_heartbeat_at >= :cutoff
                   )
                """
            ),
            {"cutoff": cutoff},
        )
    ).fetchall()
    if not stale_orgs:
        return

    from app.core.agent_gateway import revoke_all_for_org as _revoke_all_for_org  # noqa: PLC0415

    for (org_id,) in stale_orgs:
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
                reason="agent_loss",
            )
        # Revoke every active bearer for the org — agents will re-exchange
        # when they come back online.
        await _revoke_all_for_org(org_id, "agent_loss", session=s)
        log.warning("workspace.failsafe_agent_loss", org_id=str(org_id), expired_count=len(rows))


async def _attempt_destroy(row: WorkspaceRow) -> None:
    provider = _PROVIDERS.get(row.provider_id)
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
    for plugin_id, provider in _PROVIDERS.items():
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
            select(WorkspaceRow.id, WorkspaceRow.current_holder_workflow_id, WorkspaceRow.status).where(
                WorkspaceRow.current_command_id == command_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkspaceClaimState(
        workspace_id=row[0],
        current_holder_workflow_id=row[1],
        status=row[2],
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
            select(WorkspaceRow.id, WorkspaceRow.current_command_id, WorkspaceRow.status).where(
                WorkspaceRow.id == workspace_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkspaceCommandState(
        workspace_id=row[0],
        current_command_id=row[1],
        status=row[2],
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
