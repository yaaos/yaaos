"""Workspace implementation of `WorkspaceAgentReportSink`.

Owns all workspace-state access needed by agent_gateway event ingestion:
- heartbeat reconciliation (id→status map)
- workspace-event kind→status application
- claim resolution (command_id → holder_workflow_id)

Registered into agent_gateway's single-slot registry by
`core/workspace.__init__` at import time so the edge goes
workspace → agent_gateway, not the reverse.
"""

from __future__ import annotations

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

# Maps the agent-side workspace event kind to the control-plane status column.
# Only kinds listed here trigger a status write; others are no-ops.
_KIND_TO_STATUS: dict[str, str] = {
    "ready": WorkspaceStatus.ACTIVE.value,
    "destroyed": WorkspaceStatus.DESTROYED.value,
    "failed": WorkspaceStatus.DESTROY_FAILED.value,
}


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
        """Return the `current_holder_workflow_id` for the workspace holding
        `command_id`, or None when no workspace is currently claimed by it.

        Pure read — no writes.
        """
        row = (
            await session.execute(
                select(WorkspaceRow.id, WorkspaceRow.current_holder_workflow_id).where(
                    WorkspaceRow.current_command_id == command_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return row[1]

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
        """Delegate to `_failsafe_agent_loss` in service.py.

        Bridges the IoC seam so agent_gateway can trigger agent-loss cleanup
        without importing core/workspace directly.
        """
        from app.core.workspace.service import _failsafe_agent_loss  # noqa: PLC0415

        await _failsafe_agent_loss(session, agent_ids)
