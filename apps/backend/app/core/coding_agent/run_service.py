"""Run lifecycle service for coding-agent executions.

Manages `coding_agent_runs` rows + `coding_agent_activity` blobs:
- `create_run` — called from the CodeReview dispatch command (same
  transaction, status=running). Only `InvokeClaudeCode` commands get a
  run row.
- `finalize_run` — called by the coding-agent run sink on the terminal
  AgentEvent. Writes status, exit_code, tokens_in/out, duration_ms onto
  the run row, and inserts the pre-rendered `ActivityLog` JSONB blob
  into the partitioned `coding_agent_activity` table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coding_agent.models import CodingAgentActivityRow, CodingAgentRunRow
from app.core.coding_agent.types import ActivityLog, Usage

log = structlog.get_logger("core.coding_agent.run_service")


async def create_run(
    *,
    org_id: UUID,
    workflow_execution_id: UUID,
    step_id: str,
    agent_command_id: UUID,
    command_kind: str,
    plugin_id: str,
    model: str | None = None,
    effort: str | None = None,
    session: AsyncSession,
) -> UUID:
    """Insert a `coding_agent_runs` row with status=running.

    Called from the CodeReview dispatch command in the same transaction so
    the run row is durable iff the dispatch commits. Only `InvokeClaudeCode`
    commands call this — provision/cleanup/writefiles do not.

    Returns the new run id (server-minted UUIDv7 after flush).

    Required `session`; caller commits.
    """
    now = datetime.now(UTC)
    row = CodingAgentRunRow(
        org_id=org_id,
        workflow_execution_id=workflow_execution_id,
        step_id=step_id,
        agent_command_id=agent_command_id,
        command_kind=command_kind,
        plugin_id=plugin_id,
        model=model,
        effort=effort,
        status="running",
        started_at=now,
    )
    session.add(row)
    await session.flush()
    log.info(
        "coding_agent.run.created",
        run_id=str(row.id),
        org_id=str(org_id),
        agent_command_id=str(agent_command_id),
        command_kind=command_kind,
    )
    return row.id


async def finalize_run(
    run_id: UUID,
    *,
    usage: Usage,
    activity: ActivityLog | None,
    exit_code: int | None,
    status: str,
    session: AsyncSession,
) -> None:
    """Write terminal fields onto an existing run row + persist the activity blob.

    Called by the coding-agent run sink on an `InvokeClaudeCode` terminal
    AgentEvent. Writes `status`, `exit_code`, `tokens_in`, `tokens_out`,
    and `duration_ms` (preferring `usage.duration_ms` when the agent
    reported it; falling back to the wallclock delta from `started_at`
    → now). When `activity` is non-None, inserts one row into the
    partitioned `coding_agent_activity` table with the JSON-serialised
    `ActivityLog`. The run-row's `org_id` is read to tenant-stamp the
    activity row.

    Required `session`; caller commits.
    """
    now = datetime.now(UTC)

    # Read started_at + org_id together; we need both.
    row = (
        await session.execute(
            select(CodingAgentRunRow.started_at, CodingAgentRunRow.org_id).where(
                CodingAgentRunRow.id == run_id
            )
        )
    ).one_or_none()

    duration_ms: int | None = None
    org_id: UUID | None = None
    if row is not None:
        started_at, org_id = row[0], row[1]
        elapsed = now - started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else now - started_at
        duration_ms = max(0, int(elapsed.total_seconds() * 1000))

    # Prefer the agent-reported duration when present; fall back to wallclock.
    if usage.duration_ms is not None:
        duration_ms = usage.duration_ms

    await session.execute(
        update(CodingAgentRunRow)
        .where(CodingAgentRunRow.id == run_id)
        .values(
            status=status,
            exit_code=exit_code,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            duration_ms=duration_ms,
            completed_at=now,
        )
    )

    if activity is not None and org_id is not None:
        # Persist the pre-rendered activity blob into the partitioned table.
        # `created_at` is left to the server default — the partition key picks
        # the active weekly partition automatically.
        activity_row = CodingAgentActivityRow(
            run_id=run_id,
            org_id=org_id,
            payload=activity.model_dump(mode="json"),
        )
        session.add(activity_row)

    log.info(
        "coding_agent.run.finalized",
        run_id=str(run_id),
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        activity_events=len(activity.events) if activity is not None else 0,
    )


async def get_run_id_for_command(
    agent_command_id: UUID,
    *,
    session: AsyncSession,
) -> UUID | None:
    """Return the run id for an `agent_command_id`, or None if absent.

    Used by `PostFindings.execute` to populate `reviews.run_id` when
    linking the review to its run.
    """
    row = (
        await session.execute(
            select(CodingAgentRunRow.id).where(CodingAgentRunRow.agent_command_id == agent_command_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return row[0]


@dataclass(frozen=True)
class RunRef:
    """The run id + the coding-agent plugin that issued it.

    The run-sink resolves the plugin from `plugin_id` rather than a constant
    so `core/coding_agent` never hardcodes a vendor.
    """

    run_id: UUID
    plugin_id: str


async def get_run_ref_for_command(
    agent_command_id: UUID,
    *,
    session: AsyncSession,
) -> RunRef | None:
    """Return the `(run_id, plugin_id)` for an `agent_command_id`, or None.

    Used by the run-sink to resolve which plugin parses the terminal event.
    """
    row = (
        await session.execute(
            select(CodingAgentRunRow.id, CodingAgentRunRow.plugin_id).where(
                CodingAgentRunRow.agent_command_id == agent_command_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return RunRef(run_id=row[0], plugin_id=row[1])


async def get_run_id_for_workflow_step(
    workflow_execution_id: UUID,
    step_id: str,
    *,
    session: AsyncSession,
) -> UUID | None:
    """Return the run id for a given `(workflow_execution_id, step_id)`, or None.

    Used by `PostFindings.execute` to look up the run created by the
    preceding `CodeReview` step so `reviews.run_id` can be populated.
    """
    row = (
        await session.execute(
            select(CodingAgentRunRow.id).where(
                CodingAgentRunRow.workflow_execution_id == workflow_execution_id,
                CodingAgentRunRow.step_id == step_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return row[0]


async def get_step_activity(
    workflow_execution_id: UUID,
    step_id: str,
    *,
    session: AsyncSession,
) -> ActivityLog | None:
    """Return the persisted `ActivityLog` for a workflow step's coding-agent
    run, or None when there is no such run (non-`InvokeClaudeCode` step) or
    the activity row's weekly partition has been dropped (4-week TTL).

    Two-hop lookup: `(workflow_execution_id, step_id)` →
    `coding_agent_runs.id` via `get_run_id_for_workflow_step`, then
    `coding_agent_activity.payload` by `run_id`. The Activity tab in the
    SPA tolerates None as "activity expired".
    """
    run_id = await get_run_id_for_workflow_step(workflow_execution_id, step_id, session=session)
    if run_id is None:
        return None
    row = (
        await session.execute(
            select(CodingAgentActivityRow.payload).where(CodingAgentActivityRow.run_id == run_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return ActivityLog.model_validate(row[0])
