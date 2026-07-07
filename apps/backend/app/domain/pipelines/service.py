"""Service surface for `domain/pipelines`.

Definition CRUD (`create_pipeline` / `update_pipeline` / `delete_pipeline` /
`get_pipeline` / `list_pipelines` / `pipeline_referenced_by_call`) is real.
`start_run` and `request_cancel` are real, delegating the run-lifecycle
mechanics (promotion, the `ROUTE_RUN`/`START_STAGE`/`HANDLE_AGENT_EVENT`
taskiq trio, terminal bookkeeping) to `engine.py`. `start_rerun_from_stage`, `resolve_pause`,
`instantiate_template`, `list_templates`, `list_runs_for_ticket`,
`get_run_overview`, and `has_run_in_flight` stay stubs — bodies raise
`NotImplementedError`; only the signatures are load-bearing.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_pipeline
from app.core.auth import require_org_context
from app.core.tenancy import get_membership_info
from app.domain.pipelines import engine
from app.domain.pipelines.definition import PipelineDefinition, flatten, validate_definition
from app.domain.pipelines.models import PipelineRow, PipelineRunRow
from app.domain.pipelines.types import (
    Kickoff,
    PauseResolution,
    Pipeline,
    PipelineRun,
    PipelineSummary,
    RunOverview,
)
from app.domain.repos import pipeline_referenced_by_binding as _repo_pipeline_referenced_by_binding

_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "killed", "cancelled"})


class PipelineNotFoundError(LookupError):
    """No pipeline row for the given id in the current org."""


class PipelineNameTakenError(ValueError):
    """`(org_id, name)` collision on create/update."""


class PipelineReferencedError(ValueError):
    """Delete refused — a call stage or trigger binding references this pipeline."""


class RunNotFoundError(LookupError):
    """No pipeline_runs row for the given id."""


class RunAlreadyTerminalError(ValueError):
    """`request_cancel` called on a run already in a terminal state."""


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


# ---------------------------------------------------------------------------
# Definition CRUD helpers
# ---------------------------------------------------------------------------


def _definition_from_row(row: PipelineRow) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        {"id": row.id, "name": row.name, "description": row.description, "stages": row.stages}
    )


async def _load_org_definitions(org_id: UUID, *, session: AsyncSession) -> dict[UUID, PipelineDefinition]:
    rows = (await session.execute(select(PipelineRow).where(PipelineRow.org_id == org_id))).scalars().all()
    return {row.id: _definition_from_row(row) for row in rows}


async def _assert_name_available(
    org_id: UUID, name: str, *, exclude_id: UUID | None, session: AsyncSession
) -> None:
    stmt = select(PipelineRow.id).where(PipelineRow.org_id == org_id, PipelineRow.name == name)
    if exclude_id is not None:
        stmt = stmt.where(PipelineRow.id != exclude_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        raise PipelineNameTakenError(name)


async def _login_for(user_id: UUID | None, *, org_id: UUID, session: AsyncSession) -> str | None:
    if user_id is None:
        return None
    info = await get_membership_info(session, user_id=user_id, org_id=org_id)
    return info.handle if info is not None else None


async def _get_row(pipeline_id: UUID, *, org_id: UUID, session: AsyncSession) -> PipelineRow | None:
    return (
        await session.execute(
            select(PipelineRow).where(PipelineRow.id == pipeline_id, PipelineRow.org_id == org_id)
        )
    ).scalar_one_or_none()


class _PipelineCreatedPayload(BaseModel):
    name: str
    stage_count: int


class _PipelineUpdatedPayload(BaseModel):
    name: str
    stage_count: int


class _PipelineDeletedPayload(BaseModel):
    name: str


async def create_pipeline(
    *, org_id: UUID, definition: PipelineDefinition, actor: Actor, session: AsyncSession
) -> UUID:
    """Server mints missing ids: `PipelineDefinition`/`Stage` fields default
    to a fresh uuid7 at parse time, so a request body that omits `id` on the
    pipeline or any stage already carries one by the time it reaches here.
    Validation-only dry-run flatten of self + all transitive callers (there
    are none yet for a brand-new pipeline, but `validate_definition` is the
    single entry point for both create and update)."""
    org_definitions = await _load_org_definitions(org_id, session=session)
    validate_definition(definition, org_definitions=org_definitions)
    await _assert_name_available(org_id, definition.name, exclude_id=None, session=session)

    row = PipelineRow(
        id=definition.id,
        org_id=org_id,
        name=definition.name,
        description=definition.description,
        stages=[stage.model_dump(mode="json") for stage in definition.stages],
        updated_by=actor.user_id,
    )
    session.add(row)
    await session.flush()
    await audit_for_pipeline(
        row.id,
        "pipeline.created",
        _PipelineCreatedPayload(name=definition.name, stage_count=len(definition.stages)),
        actor=actor,
        org_id=org_id,
        session=session,
    )
    return row.id


async def update_pipeline(
    pipeline_id: UUID, *, definition: PipelineDefinition, actor: Actor, session: AsyncSession
) -> None:
    """Replace stored definition (applies to new runs only). Revalidates the
    edited pipeline plus every pipeline that transitively calls it — a
    cycle or name collision introduced anywhere in that set is rejected."""
    org_id = require_org_context()
    row = await _get_row(pipeline_id, org_id=org_id, session=session)
    if row is None:
        raise PipelineNotFoundError(pipeline_id)

    # The path's pipeline_id is authoritative; ignore whatever `id` the
    # request body carried (or the fresh one default_factory minted).
    definition = definition.model_copy(update={"id": pipeline_id})
    org_definitions = await _load_org_definitions(org_id, session=session)
    validate_definition(definition, org_definitions=org_definitions)
    await _assert_name_available(org_id, definition.name, exclude_id=pipeline_id, session=session)

    row.name = definition.name
    row.description = definition.description
    row.stages = [stage.model_dump(mode="json") for stage in definition.stages]
    row.updated_by = actor.user_id
    await session.flush()
    await audit_for_pipeline(
        pipeline_id,
        "pipeline.updated",
        _PipelineUpdatedPayload(name=definition.name, stage_count=len(definition.stages)),
        actor=actor,
        org_id=org_id,
        session=session,
    )


async def delete_pipeline(pipeline_id: UUID, *, actor: Actor, session: AsyncSession) -> None:
    """Raises `PipelineReferencedError` if a call stage or trigger binding
    references it."""
    org_id = require_org_context()
    row = await _get_row(pipeline_id, org_id=org_id, session=session)
    if row is None:
        raise PipelineNotFoundError(pipeline_id)
    if await pipeline_referenced_by_call(
        pipeline_id, session=session
    ) or await _repo_pipeline_referenced_by_binding(pipeline_id, session=session):
        raise PipelineReferencedError(pipeline_id)

    name = row.name
    await session.delete(row)
    await session.flush()
    await audit_for_pipeline(
        pipeline_id,
        "pipeline.deleted",
        _PipelineDeletedPayload(name=name),
        actor=actor,
        org_id=org_id,
        session=session,
    )


async def get_pipeline(pipeline_id: UUID, *, session: AsyncSession) -> Pipeline:
    org_id = require_org_context()
    row = await _get_row(pipeline_id, org_id=org_id, session=session)
    if row is None:
        raise PipelineNotFoundError(pipeline_id)
    referenced = await pipeline_referenced_by_call(
        pipeline_id, session=session
    ) or await _repo_pipeline_referenced_by_binding(pipeline_id, session=session)
    return Pipeline(
        definition=_definition_from_row(row),
        updated_at=row.updated_at,
        updated_by_login=await _login_for(row.updated_by, org_id=org_id, session=session),
        referenced=referenced,
    )


async def list_pipelines(org_id: UUID, *, session: AsyncSession) -> list[PipelineSummary]:
    rows = (
        (
            await session.execute(
                select(PipelineRow).where(PipelineRow.org_id == org_id).order_by(PipelineRow.name)
            )
        )
        .scalars()
        .all()
    )
    out: list[PipelineSummary] = []
    for row in rows:
        referenced = await pipeline_referenced_by_call(
            row.id, session=session
        ) or await _repo_pipeline_referenced_by_binding(row.id, session=session)
        out.append(
            PipelineSummary(
                id=row.id,
                name=row.name,
                stage_count=len(row.stages),
                updated_at=row.updated_at,
                updated_by_login=await _login_for(row.updated_by, org_id=org_id, session=session),
                referenced=referenced,
            )
        )
    return out


async def pipeline_referenced_by_call(pipeline_id: UUID, *, session: AsyncSession) -> bool:
    """True iff another pipeline in the current org has a `call` stage whose
    `pipeline_id` targets `pipeline_id`. App-side JSONB scan — the org's
    pipeline count is small, and this avoids a Postgres-dialect-specific
    JSONB containment query for a POC-scale table."""
    org_id = require_org_context()
    rows = (
        (
            await session.execute(
                select(PipelineRow.stages).where(PipelineRow.org_id == org_id, PipelineRow.id != pipeline_id)
            )
        )
        .scalars()
        .all()
    )
    target = str(pipeline_id)
    for stages in rows:
        for stage in stages:
            if stage.get("kind") == "call" and stage.get("pipeline_id") == target:
                return True
    return False


# ---------------------------------------------------------------------------
# Run lifecycle — start_run / request_cancel real; the rest still stubs
# ---------------------------------------------------------------------------


async def start_run(
    *,
    org_id: UUID,
    ticket_id: UUID,
    pipeline_id: UUID,
    kickoff: Kickoff,
    session: AsyncSession,
) -> UUID:
    """Flatten against CURRENT definitions, pin the snapshot; queue if a run
    is already in flight on the ticket, else attempt immediate promotion.

    Always inserts `state='queued'` first, then calls `engine.attempt_promotion`
    — a conditional `queued -> running` flip guarded by the
    `ux_pipeline_runs_one_in_flight` partial unique index — so a race against
    a concurrent terminal's own promotion of an older queued run can never
    leave two runs `running` on the same ticket.
    """
    org_definitions = await _load_org_definitions(org_id, session=session)
    definition = org_definitions.get(pipeline_id)
    if definition is None:
        raise PipelineNotFoundError(pipeline_id)
    flattened = flatten(definition, org_definitions=org_definitions)

    row = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=pipeline_id,
        pipeline_name=definition.name,
        definition_snapshot={"stages": [stage.model_dump(mode="json") for stage in flattened.stages]},
        state="queued",
        phase="stages",
        kickoff=kickoff.model_dump(mode="json"),
    )
    session.add(row)
    await session.flush()
    await engine.attempt_promotion(row, session=session)
    return row.id


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
    Queued: cancel directly. Raises `RunAlreadyTerminalError` on a run
    already in a terminal state (mapped to 409 at HTTP)."""
    org_id = require_org_context()
    run = (
        await session.execute(
            select(PipelineRunRow).where(PipelineRunRow.id == run_id, PipelineRunRow.org_id == org_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise RunNotFoundError(run_id)
    if run.state in _TERMINAL_RUN_STATES:
        raise RunAlreadyTerminalError(run_id)
    if run.state == "queued":
        await engine.cancel_queued(run, actor=actor, session=session)
        return
    if run.state == "running":
        run.cancel_requested = True
        return
    # `paused` cancel-immediately semantics land with the pause/resolve
    # machinery — no pause can exist yet (boundary evaluation always
    # proceeds), so this branch is unreachable today.
    raise NotImplementedError("cancel of a paused run lands with the pause/resolve machinery")


async def resolve_pause(
    pause_id: UUID,
    *,
    resolution: PauseResolution,
    actor: Actor,
    session: AsyncSession,
) -> None:
    """The single HITL resolution entry."""
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


async def list_runs_for_ticket(ticket_id: UUID, *, session: AsyncSession) -> list[PipelineRun]:
    """Runs-tab timeline (newest first; no pagination)."""
    raise NotImplementedError


async def get_run_overview(ticket_id: UUID, *, session: AsyncSession) -> RunOverview | None:
    raise NotImplementedError


async def has_run_in_flight(ticket_id: UUID, *, session: AsyncSession) -> bool:
    """`pr_review` batching gate.

    Returns True if any `pipeline_runs` row for `ticket_id` is in a
    non-terminal active state: `queued`, `running`, or `paused`.

    `queued` is included because it means the ticket already has a `running`
    or `paused` run holding the one-in-flight slot (the
    `ux_pipeline_runs_one_in_flight` index guarantees this) plus at least one
    run waiting to follow — the ticket is committed to pipeline work and
    starting another run would be redundant.
    """
    row = (
        await session.execute(
            select(PipelineRunRow.id).where(
                PipelineRunRow.ticket_id == ticket_id,
                PipelineRunRow.state.in_(["queued", "running", "paused"]),
            )
        )
    ).first()
    return row is not None
