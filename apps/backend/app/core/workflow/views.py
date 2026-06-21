"""Read projections for `core/workflow`.

`WorkflowExecutionSummary`, `WorkflowRunView`, `HitlHistoryEntry`, and the
`list_*` / `get_*` query ops live here so `service.py` stays focused on the
engine and its three task bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow.models import PendingHumanDecisionRow, WorkflowExecutionRow
from app.core.workflow.types import TERMINAL_STATES, StepRef, WorkflowNotFoundError, WorkflowState


@dataclass(frozen=True)
class WorkflowExecutionSummary:
    id: UUID
    ticket_id: UUID
    workflow_name: str
    state: str
    current_step_id: str | None
    created_at: datetime
    updated_at: datetime
    pending_agent_command_id: UUID | None = None
    cancel_requested: bool = False
    otel_trace_context: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class HitlHistoryEntry:
    id: UUID
    workflow_execution_id: UUID
    question_payload: dict[str, Any]
    resolution_payload: dict[str, Any] | None
    resolved_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class _StepSummary:
    """One step's projected state — private to this module; accessed via WorkflowRunView.steps."""

    step_id: str
    command_kind: str
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class WorkflowRunView:
    id: UUID
    workflow_name: str
    workflow_version: int
    state: str
    current_step_id: str | None
    created_at: datetime
    updated_at: datetime
    steps: tuple[_StepSummary, ...]
    failure_reason: str | None = None


def _project_execution(row: WorkflowExecutionRow) -> WorkflowExecutionSummary:
    return WorkflowExecutionSummary(
        id=row.id,
        ticket_id=row.ticket_id,
        workflow_name=row.workflow_name,
        state=row.state,
        current_step_id=row.current_step_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        pending_agent_command_id=row.pending_agent_command_id,
        cancel_requested=row.cancel_requested,
        otel_trace_context=row.otel_trace_context,
        failure_reason=row.failure_reason,
    )


async def get_execution_summary(
    execution_id: UUID, *, session: AsyncSession
) -> WorkflowExecutionSummary | None:
    row = await session.get(WorkflowExecutionRow, execution_id)
    if row is None:
        return None
    return _project_execution(row)


async def get_awaiting_human_execution(
    ticket_id: UUID, *, session: AsyncSession
) -> WorkflowExecutionSummary | None:
    from sqlalchemy import desc  # noqa: PLC0415

    row = (
        await session.execute(
            select(WorkflowExecutionRow)
            .where(
                WorkflowExecutionRow.ticket_id == ticket_id,
                WorkflowExecutionRow.state == WorkflowState.AWAITING_HUMAN.value,
            )
            .order_by(desc(WorkflowExecutionRow.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return _project_execution(row)


async def list_active_execution_ids(ticket_id: UUID, *, session: AsyncSession) -> list[UUID]:
    rows = (
        (
            await session.execute(
                select(WorkflowExecutionRow.id).where(
                    WorkflowExecutionRow.ticket_id == ticket_id,
                    WorkflowExecutionRow.state.notin_([st.value for st in TERMINAL_STATES]),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def list_hitl_history(ticket_id: UUID, *, session: AsyncSession) -> list[HitlHistoryEntry]:
    from sqlalchemy import desc  # noqa: PLC0415

    wfx_ids = (
        (
            await session.execute(
                select(WorkflowExecutionRow.id).where(WorkflowExecutionRow.ticket_id == ticket_id)
            )
        )
        .scalars()
        .all()
    )
    if not wfx_ids:
        return []
    rows = (
        (
            await session.execute(
                select(PendingHumanDecisionRow)
                .where(PendingHumanDecisionRow.workflow_execution_id.in_(wfx_ids))
                .order_by(desc(PendingHumanDecisionRow.created_at))
            )
        )
        .scalars()
        .all()
    )
    return [
        HitlHistoryEntry(
            id=r.id,
            workflow_execution_id=r.workflow_execution_id,
            question_payload=r.question_payload,
            resolution_payload=r.resolution_payload,
            resolved_at=r.resolved_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _step_summary(
    step: StepRef,
    *,
    is_current: bool,
    entry: dict[str, Any] | None,
    execution_state: str,
) -> _StepSummary:
    started = _parse_iso(entry.get("started_at")) if isinstance(entry, dict) else None
    completed = _parse_iso(entry.get("completed_at")) if isinstance(entry, dict) else None
    outcome_label: str | None = entry.get("outcome_label") if isinstance(entry, dict) else None

    if outcome_label == "success":
        state = "done"
    elif outcome_label is not None:
        state = "skipped" if outcome_label == "_skipped" else "failed"
    elif is_current and execution_state in {
        WorkflowState.RUNNING.value,
        WorkflowState.AWAITING_AGENT.value,
        WorkflowState.AWAITING_HUMAN.value,
    }:
        state = "running"
    else:
        state = "pending"

    return _StepSummary(
        step_id=step.step_id,
        command_kind=step.command_class.kind,
        state=state,
        started_at=started,
        completed_at=completed,
    )


def _project_run_view(row: WorkflowExecutionRow) -> WorkflowRunView:
    # Lazy import breaks the circular dependency: views imports service for
    # get_engine; service does not import views.
    from app.core.workflow.service import get_engine  # noqa: PLC0415

    engine = get_engine()
    try:
        wf = engine.get_workflow(row.workflow_name, version=row.workflow_version)
    except WorkflowNotFoundError:
        steps: tuple[_StepSummary, ...] = ()
    else:
        summaries: list[_StepSummary] = []
        for step in wf.steps:
            entry = row.step_state.get(step.step_id)
            if not isinstance(entry, dict):
                entry = None
            summaries.append(
                _step_summary(
                    step,
                    is_current=(row.current_step_id == step.step_id),
                    entry=entry,
                    execution_state=row.state,
                )
            )
        steps = tuple(summaries)
    return WorkflowRunView(
        id=row.id,
        workflow_name=row.workflow_name,
        workflow_version=row.workflow_version,
        state=row.state,
        current_step_id=row.current_step_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        steps=steps,
        failure_reason=row.failure_reason,
    )


async def list_run_views_for_ticket(ticket_id: UUID, *, session: AsyncSession) -> list[WorkflowRunView]:
    from sqlalchemy import desc  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(WorkflowExecutionRow)
                .where(WorkflowExecutionRow.ticket_id == ticket_id)
                .order_by(desc(WorkflowExecutionRow.created_at))
            )
        )
        .scalars()
        .all()
    )
    return [_project_run_view(r) for r in rows]


async def list_workflow_states(*, session: AsyncSession) -> list[str]:
    """Return all `workflow_executions.state` values — for org-scoped status counts."""
    return list((await session.execute(select(WorkflowExecutionRow.state))).scalars().all())
