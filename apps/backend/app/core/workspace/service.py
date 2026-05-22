"""Lifecycle service + registry + reaper for `core/workspace`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, select, update

from app.core.database import session as get_session
from app.core.observability import spawn
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import (
    CodingAgentCliResult,
    HealthStatus,
    OnStreamLine,
    Workspace,
    WorkspaceError,
    WorkspaceInfo,
    WorkspaceNotFoundError,
    WorkspaceProvider,
    WorkspaceProvisionError,
    WorkspaceSpec,
    WorkspaceStatus,
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


def _reset_providers_for_tests() -> None:
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
        await s.commit()

    log.info("workspace.created", workspace_id=str(ws_id), provider_id=provider_id)
    return _WorkspaceImpl(id=str(ws_id), provider=provider, plugin_state=plugin_state)


async def close_workspace(workspace_id: UUID) -> None:
    """Mark the workspace expired so the reaper picks it up. Idempotent."""
    async with get_session() as s:
        await s.execute(
            update(WorkspaceRow)
            .where(
                WorkspaceRow.id == workspace_id,
                WorkspaceRow.status.in_([WorkspaceStatus.ACTIVE.value, WorkspaceStatus.CREATING.value]),
            )
            .values(status=WorkspaceStatus.EXPIRED.value)
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


async def force_close_all(*, org_id: UUID) -> int:
    """Flip every active/creating workspace for the org to expired. Returns count."""
    async with get_session() as s:
        result = await s.execute(
            update(WorkspaceRow)
            .where(
                WorkspaceRow.org_id == org_id,
                WorkspaceRow.status.in_([WorkspaceStatus.ACTIVE.value, WorkspaceStatus.CREATING.value]),
            )
            .values(status=WorkspaceStatus.EXPIRED.value)
        )
        await s.commit()
        return result.rowcount or 0


async def _reaper_sweep_once() -> None:
    """One reaper pass — expire over-budget, idle-timeout, destroy expired,
    mark stuck failed."""
    now = _utcnow()
    async with get_session() as s:
        # 1. Expire over-budget actives (TTL sweep).
        await s.execute(
            update(WorkspaceRow)
            .where(
                WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
                WorkspaceRow.expires_at < now,
            )
            .values(status=WorkspaceStatus.EXPIRED.value)
        )
        # 1b. Idle-timeout sweep (M05 Phase 3): an `active` workspace with no
        # current claim that's been activated longer than `max_idle_seconds`
        # is considered abandoned. Marked expired so the destroy pass picks
        # it up. Workspaces with a live claim are skipped — the engine owns
        # them; cancellation flows through `workflow.request_cancel`.
        await s.execute(
            update(WorkspaceRow)
            .where(
                WorkspaceRow.status == WorkspaceStatus.ACTIVE.value,
                WorkspaceRow.current_command_id.is_(None),
                WorkspaceRow.activated_at.is_not(None),
                func.extract("epoch", now - WorkspaceRow.activated_at) > WorkspaceRow.max_idle_seconds,
            )
            .values(status=WorkspaceStatus.EXPIRED.value)
        )
        # 2. Find rows to destroy
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
