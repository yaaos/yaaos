"""Run lifecycle service for coding-agent executions.

Manages `coding_agent_runs` rows:
- `create_run` — called from the CodeReview dispatch command (same
  transaction, status=running). Only `InvokeClaudeCode` commands get a
  run row.
- `finalize_run` — called by the coding-agent run sink on the terminal
  AgentEvent. Writes status/exit_code/duration_ms; tokens_in/out are
  NULL until usage-parsing is wired.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.coding_agent.models import CodingAgentRunRow

log = structlog.get_logger("domain.coding_agent.run_service")


async def create_run(
    *,
    org_id: UUID,
    workflow_execution_id: UUID,
    step_id: str,
    agent_command_id: UUID,
    command_kind: str,
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
    usage: object,  # Usage | None
    activity: object,  # ActivityLog | None
    exit_code: int | None,
    status: str,
    session: AsyncSession,
) -> None:
    """Write terminal fields onto an existing run row.

    Called by the coding-agent run sink on an `InvokeClaudeCode` terminal
    AgentEvent. Writes `status`, `exit_code`, and `duration_ms` (derived
    from `started_at` → now). `tokens_in`/`tokens_out` remain NULL.

    The `usage` and `activity` parameters are reserved for future use;
    callers pass `None` and the values are ignored.

    Required `session`; caller commits.
    """
    del usage, activity  # reserved for future use; not consumed today

    now = datetime.now(UTC)

    # Read started_at to compute duration.
    row = (
        await session.execute(select(CodingAgentRunRow.started_at).where(CodingAgentRunRow.id == run_id))
    ).one_or_none()

    duration_ms: int | None = None
    if row is not None:
        elapsed = now - row[0].replace(tzinfo=UTC) if row[0].tzinfo is None else now - row[0]
        duration_ms = max(0, int(elapsed.total_seconds() * 1000))

    await session.execute(
        update(CodingAgentRunRow)
        .where(CodingAgentRunRow.id == run_id)
        .values(
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            completed_at=now,
            # tokens_in / tokens_out remain NULL until usage parsing is wired
        )
    )
    log.info(
        "coding_agent.run.finalized",
        run_id=str(run_id),
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
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
