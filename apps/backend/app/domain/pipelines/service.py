"""Stub service surface for `domain/pipelines`.

Bodies raise `NotImplementedError` — only the signatures are load-bearing.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.domain.pipelines.types import (
    Kickoff,
    PauseResolution,
    Pipeline,
    PipelineDefinition,
    PipelineRun,
    PipelineSummary,
    RunOverview,
)


class PipelineNotFoundError(LookupError):
    """No pipeline row for the given id."""


class PipelineValidationError(ValueError):
    """Flatten-time collision or cycle across a pipeline + its transitive callers."""


class PipelineReferencedError(ValueError):
    """Delete refused — a call stage or trigger binding references this pipeline."""


class RunNotFoundError(LookupError):
    """No pipeline_runs row for the given id."""


class PauseNotFoundError(LookupError):
    """No run_pauses row for the given id."""


class PauseAlreadyResolvedError(ValueError):
    """`resolve_pause` called on a pause that already has a resolution."""


class NotEscalationTargetError(PermissionError):
    """Actor is not in the pause's escalation set nor an org admin."""


class StageNotInDefinitionError(LookupError):
    """`start_rerun_from_stage`'s `from_stage` isn't in the current definition."""


class MissingInheritedArtifactError(ValueError):
    """A renamed/removed earlier stage leaves a required input artifact unresolvable."""


async def start_run(
    *,
    org_id: UUID,
    ticket_id: UUID,
    pipeline_id: UUID,
    kickoff: Kickoff,
    session: AsyncSession,
) -> UUID:
    """Flatten against CURRENT definitions, pin the snapshot; queue if a run
    is already in flight on the ticket, else attempt immediate promotion."""
    raise NotImplementedError


async def start_rerun_from_stage(
    *,
    org_id: UUID,
    ticket_id: UUID,
    from_stage: str,
    instruction: str,
    actor: Actor,
    session: AsyncSession,
) -> UUID:
    """New run on the CURRENT definition, starting at `from_stage`'s index."""
    raise NotImplementedError


async def request_cancel(run_id: UUID, *, actor: Actor, session: AsyncSession) -> None:
    """Running: cancel at the next boundary. Paused: cancel immediately.
    Queued: cancel directly. No-op on terminal runs."""
    raise NotImplementedError


async def resolve_pause(
    pause_id: UUID,
    *,
    resolution: PauseResolution,
    actor: Actor,
    session: AsyncSession,
) -> None:
    """The single HITL resolution entry."""
    raise NotImplementedError


async def create_pipeline(
    *, org_id: UUID, definition: PipelineDefinition, actor: Actor, session: AsyncSession
) -> UUID:
    """Server mints missing stage ids; validation-only dry-run flatten of
    self + all transitive callers."""
    raise NotImplementedError


async def update_pipeline(
    pipeline_id: UUID, *, definition: PipelineDefinition, actor: Actor, session: AsyncSession
) -> None:
    raise NotImplementedError


async def delete_pipeline(pipeline_id: UUID, *, actor: Actor, session: AsyncSession) -> None:
    """Raises `PipelineReferencedError` if a call stage or trigger binding references it."""
    raise NotImplementedError


async def get_pipeline(pipeline_id: UUID, *, session: AsyncSession) -> Pipeline:
    raise NotImplementedError


async def list_pipelines(org_id: UUID, *, session: AsyncSession) -> list[PipelineSummary]:
    raise NotImplementedError


async def instantiate_template(
    *, org_id: UUID, template_id: UUID, actor: Actor, session: AsyncSession
) -> UUID:
    """Deep copy of a shipped default: fresh pipeline + stage ids, call
    targets rewired depth-first."""
    raise NotImplementedError


def list_templates() -> tuple[PipelineDefinition, ...]:
    """The code-shipped default pipelines (pinned ids)."""
    raise NotImplementedError


async def pipeline_referenced_by_call(pipeline_id: UUID, *, session: AsyncSession) -> bool:
    raise NotImplementedError


async def list_runs_for_ticket(ticket_id: UUID, *, session: AsyncSession) -> list[PipelineRun]:
    """Runs-tab timeline (newest first; no pagination)."""
    raise NotImplementedError


async def get_run_overview(ticket_id: UUID, *, session: AsyncSession) -> RunOverview | None:
    raise NotImplementedError


async def has_run_in_flight(ticket_id: UUID, *, session: AsyncSession) -> bool:
    """`pr_review` batching gate."""
    raise NotImplementedError
