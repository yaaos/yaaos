"""Workspace implementation of `WorkspaceAgentReportSink`.

Owns all workspace-state access needed by agent_gateway event ingestion:
- heartbeat reconciliation (id→status map)
- workspace-event kind→status application + lean row creation on first event
- claim resolution (command_id → holder_workflow_id)
- claim release on terminal agent events (failure-report-precedes-disposal)

Registered into agent_gateway's single-slot registry by
`core/workspace.__init__` at import time so the edge goes
workspace → agent_gateway, not the reverse.

Lean workspace row creation: when the agent reports a `created` or `ready`
workspace event and no `workspaces` row exists yet, this module creates the
row with `status='active'`, `owning_agent_id` from the reporting bearer, and
`org_id`/`spec` resolved from the originating `agent_commands` row
(by `command_id`). The `workspace_id` is minted up front in
`ProvisionWorkspace.dispatch`; the row materialises only once an agent owns it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    WorkspaceEventOutcome,
    WorkspaceEventReport,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus

log = structlog.get_logger("core.workspace.agent_report")

# Remote-provider id for lean-created rows.
_REMOTE_PROVIDER_ID = "remote_agent"

# Default workspace TTL in seconds for lean-created rows; matches
# `ProvisionWorkspace` dispatch defaults.
_DEFAULT_TTL_SECONDS = 600

# Maps the agent-side workspace event kind to the control-plane status column.
# Only kinds listed here trigger a status write; others are no-ops.
_KIND_TO_STATUS: dict[str, str] = {
    "ready": WorkspaceStatus.ACTIVE.value,
    "destroyed": WorkspaceStatus.DESTROYED.value,
    "failed": WorkspaceStatus.DESTROY_FAILED.value,
}

# Workspace event kinds that trigger lean row creation when the row is absent.
_ROW_CREATE_KINDS = frozenset({"created", "ready"})


class WorkspaceAgentReportSinkImpl:
    """Concrete implementation of the `WorkspaceAgentReportSink` Protocol."""

    async def reconcile_heartbeat(
        self,
        reported_ids: set[UUID],
        session: AsyncSession,
    ) -> set[UUID]:
        """Return workspace ids the agent reports that the control plane no
        longer tracks (row missing) or has marked `destroyed`.

        Pure read — no writes.
        """
        if not reported_ids:
            return set()
        rows = (
            await session.execute(
                select(WorkspaceRow.id, WorkspaceRow.status).where(WorkspaceRow.id.in_(reported_ids))
            )
        ).all()
        known = {row[0]: row[1] for row in rows}
        forgotten: set[UUID] = set()
        for ws_id in reported_ids:
            status = known.get(ws_id)
            if status is None or status == WorkspaceStatus.DESTROYED.value:
                forgotten.add(ws_id)
        return forgotten

    async def apply_workspace_event(
        self,
        report: WorkspaceEventReport,
        session: AsyncSession,
    ) -> WorkspaceEventOutcome:
        """Apply the agent-reported event kind to workspace status.

        Lean row creation: when the event kind is `created` or `ready` and no
        `workspaces` row exists for `report.workspace_id`, this method creates
        the row with `status='active'`, `owning_agent_id` from the reporting
        bearer (passed as `report.agent_id`), and `org_id`/`spec` resolved from
        the originating `agent_commands` row via `report.command_id`. The lean
        path never goes through `creating` — the row is inserted active.

        Validates the stale-claim guard: if the workspace's current_command_id
        doesn't match event.command_id (and it isn't None), the event is rejected
        with accepted=False — the caller maps this to a 410 response.

        Returns WorkspaceEventOutcome; never raises across the boundary.
        """
        row = (
            await session.execute(
                select(WorkspaceRow.id, WorkspaceRow.current_command_id, WorkspaceRow.status).where(
                    WorkspaceRow.id == report.workspace_id
                )
            )
        ).one_or_none()

        if row is None:
            # Lean creation: insert the workspace row on first event.
            if report.kind in _ROW_CREATE_KINDS:
                created = await _create_lean_workspace_row(report, session)
                if created:
                    log.info(
                        "workspace.lean_row_created",
                        workspace_id=str(report.workspace_id),
                        kind=report.kind,
                        agent_id=str(report.agent_id) if report.agent_id else None,
                    )
                    return WorkspaceEventOutcome(resolved_status=WorkspaceStatus.ACTIVE.value, accepted=True)
            return WorkspaceEventOutcome(resolved_status=None, accepted=False)

        _ws_id, current_command_id, current_status = row

        # Stale-claim guard: reject if command_id mismatches (but allow None
        # current_command_id so events for workspaces without an active claim
        # can still apply status transitions like "destroyed").
        if current_command_id is not None and current_command_id != report.command_id:
            return WorkspaceEventOutcome(resolved_status=current_status, accepted=False)

        new_status = _KIND_TO_STATUS.get(report.kind)
        if new_status is not None:
            await session.execute(
                update(WorkspaceRow).where(WorkspaceRow.id == report.workspace_id).values(status=new_status)
            )
        log.info(
            "workspace.agent_event_applied",
            workspace_id=str(report.workspace_id),
            kind=report.kind,
            new_status=new_status,
        )
        return WorkspaceEventOutcome(resolved_status=new_status, accepted=True)

    async def resolve_claim(
        self,
        command_id: UUID,
        session: AsyncSession,
    ) -> UUID | None:
        """Return the `workflow_execution_id` for `command_id`, or None when
        no agent_commands row exists or has no workflow correlation.

        Correlation lives on `agent_commands.workflow_execution_id` — the
        shed `workspaces.current_holder_workflow_id` column is no longer read.
        Pure read — no writes.
        """
        from app.core.agent_gateway import get_command_workflow_execution_id  # noqa: PLC0415

        return await get_command_workflow_execution_id(command_id, session=session)

    async def release_command_claim(
        self,
        command_id: UUID,
        session: AsyncSession,
    ) -> None:
        """Release the single-flight claim on whichever workspace holds
        `command_id` by clearing `current_command_id`. Called on every
        terminal agent event before the workflow engine is resumed —
        failure-report-precedes-disposal ordering.

        No-op when no workspace holds the command (e.g. `ProvisionWorkspace`
        before the lean row exists, or an agent-scoped command that has
        no associated workspace row).
        """
        from app.core.workspace.dispatch import release_claim  # noqa: PLC0415

        # Resolve workspace_id for the command from the workspace row that
        # currently holds the claim. `release_claim` requires the workspace_id.
        row = (
            await session.execute(
                select(WorkspaceRow.id).where(WorkspaceRow.current_command_id == command_id)
            )
        ).one_or_none()
        if row is None:
            # No workspace holds this command — normal for ProvisionWorkspace
            # before the lean row exists, or for agent-scoped commands.
            return
        workspace_id = row[0]
        released = await release_claim(workspace_id, command_id=command_id, session=session)
        if released:
            log.info(
                "workspace.claim_released",
                workspace_id=str(workspace_id),
                command_id=str(command_id),
            )

    async def owning_agent_for_workspace(
        self,
        workspace_id: UUID,
        session: AsyncSession,
    ) -> UUID | None:
        """Return the owning agent id (`workspace_agents.id`) for `workspace_id`,
        or None when the row is missing or its `owning_agent_id` is NULL. Pure
        read — no writes."""
        row = (
            await session.execute(select(WorkspaceRow.owning_agent_id).where(WorkspaceRow.id == workspace_id))
        ).one_or_none()
        if row is None:
            return None
        return row[0]

    async def owning_agent_for_command(
        self,
        command_id: UUID,
        session: AsyncSession,
    ) -> UUID | None:
        """Return the owning agent id for the workspace holding `command_id`,
        or None when no workspace holds it or its `owning_agent_id` is NULL. Pure
        read — no writes."""
        row = (
            await session.execute(
                select(WorkspaceRow.owning_agent_id).where(WorkspaceRow.current_command_id == command_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return row[0]

    async def handle_agent_loss(
        self,
        agent_ids: set[UUID],
        session: AsyncSession,
    ) -> None:
        """Delegate to `failsafe_agent_loss` in service.py.

        Bridges the IoC seam so agent_gateway can trigger agent-loss cleanup
        without importing core/workspace directly.
        """
        from app.core.workspace.service import failsafe_agent_loss  # noqa: PLC0415

        await failsafe_agent_loss(session, agent_ids)


async def _create_lean_workspace_row(
    report: WorkspaceEventReport,
    session: AsyncSession,
) -> bool:
    """Insert a lean `workspaces` row on the agent's first workspace event.

    Resolves `org_id` and `spec` from the originating `agent_commands` row
    (by `command_id`). `owning_agent_id` comes from the reporting bearer
    (`report.agent_id`). The row is inserted with `status='active'` — no
    `creating` intermediate state.

    Returns True when the row was inserted, False when `command_id` is None
    or the `agent_commands` row is not found (no source to derive data from).
    """
    if report.command_id is None:
        return False

    from app.core.agent_gateway import get_command_org_and_payload  # noqa: PLC0415

    result = await get_command_org_and_payload(report.command_id, session=session)
    if result is None:
        return False

    org_id, cmd_payload = result
    now = datetime.now(UTC)
    ws_row = WorkspaceRow(
        id=report.workspace_id,
        org_id=org_id,
        owning_agent_id=report.agent_id,
        provider_id=_REMOTE_PROVIDER_ID,
        spec=cmd_payload,
        status=WorkspaceStatus.ACTIVE.value,
        current_command_id=None,
        activated_at=now,
        expires_at=now + timedelta(seconds=_DEFAULT_TTL_SECONDS),
        max_idle_seconds=_DEFAULT_TTL_SECONDS,
    )
    session.add(ws_row)
    await session.flush()
    return True
