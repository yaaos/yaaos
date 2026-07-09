"""Service surface for `domain/pipelines`.

Definition CRUD (`create_pipeline` / `update_pipeline` / `delete_pipeline` /
`get_pipeline` / `list_pipelines` / `pipeline_referenced_by_call`), the run
lifecycle (`start_run` / `request_cancel`), every pause resolution +
re-entry path (`resolve_pause` — approve/instruct/send_back/kill —
`start_rerun_from_stage`), and the shipped-default template surface
(`list_templates` / `instantiate_template`, backed by `defaults.py`) are all
real. Run-engine mechanics (promotion, the
`ROUTE_RUN`/`START_STAGE`/`HANDLE_AGENT_EVENT` taskiq trio, the send-back
rewind, terminal bookkeeping) live in `engine.py`; the read models
(`list_runs_for_ticket`, `get_run_overview`) live in `views.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid7

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit, audit_for_pipeline
from app.core.auth import require_org_context
from app.core.tenancy import get_membership_info
from app.domain.artifacts import latest_final
from app.domain.pipelines import defaults, engine
from app.domain.pipelines.definition import (
    FlattenedDefinition,
    PipelineCallStage,
    PipelineDefinition,
    ReviewSkillStage,
    SkillStage,
    Stage,
    flatten,
    validate_definition,
)
from app.domain.pipelines.escalation import is_pause_responder
from app.domain.pipelines.models import PipelineRow, PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.pipelines.types import Kickoff, PauseResolution, Pipeline, PipelineSummary, RevisionContext
from app.domain.repos import PipelineRef
from app.domain.repos import pipeline_referenced_by_binding as _repo_pipeline_referenced_by_binding
from app.domain.tickets import get as get_ticket

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


class TemplateNotFoundError(LookupError):
    """No shipped default `PipelineDefinition` for the given id."""


class InvalidPauseResolutionError(ValueError):
    """`resolve_pause` called with a resolution invalid for its own `action`:
    `instruct` without `instruction`, `send_back` without
    `send_back_to_stage`, or a `send_back_to_stage` that doesn't resolve to
    an upstream `SkillStage` in the paused stage's own flattened definition."""


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


async def _lookup_pipeline_ref(pipeline_id: UUID, session: AsyncSession) -> PipelineRef | None:
    """Registered with `domain/repos.register_pipeline_lookup` at import
    time (see `apps/backend/app/domain/pipelines/__init__.py`) — the org +
    name a `TriggerBindingSpec`'s `pipeline_id` resolves to, without
    `domain/repos` importing this module directly (would cycle)."""
    row = await session.get(PipelineRow, pipeline_id)
    if row is None:
        return None
    return PipelineRef(org_id=row.org_id, name=row.name)


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


async def _nearest_upstream_skill_stage_resolvable(
    flattened: FlattenedDefinition, *, org_id: UUID, ticket_id: UUID, from_index: int, session: AsyncSession
) -> None:
    """Mirrors `engine._resolve_stage_input`'s backward walk exactly: the
    nearest upstream `SkillStage` whose `latest_final` resolves wins; if
    NONE of the upstream skill stages resolve (a renamed/removed stage under
    current names), raise loudly rather than let the rerun silently fall
    back to the kickoff's (here, empty) input text. No upstream skill stage
    at all is not an error — `from_stage` is then effectively a first stage.
    """
    nearest_name: str | None = None
    for prior in reversed(flattened.stages[:from_index]):
        if not isinstance(prior, SkillStage):
            continue
        if nearest_name is None:
            nearest_name = prior.name
        final = await latest_final(org_id=org_id, ticket_id=ticket_id, stage_name=prior.name, session=session)
        if final is not None:
            return
    if nearest_name is not None:
        raise MissingInheritedArtifactError(nearest_name)


async def start_rerun_from_stage(
    *,
    org_id: UUID,
    ticket_id: UUID,
    from_stage: str,
    instruction: str,
    actor: Actor,
    session: AsyncSession,
) -> UUID:
    """New run on the CURRENT definition, starting at `from_stage`'s index.
    Earlier stages' artifacts are read through (`latest_final` per stage
    name, no copy) — the ticket's most recent run's pipeline is re-flattened
    fresh against the org's CURRENT definitions (not that run's pinned
    snapshot), so a since-edited pipeline is what actually executes.

    Raises `RunNotFoundError` when the ticket has no prior run to rerun
    (nothing to derive a pipeline from); `StageNotInDefinitionError` when
    `from_stage` (or the prior run's pipeline itself) isn't in the current
    definition; `MissingInheritedArtifactError` when a renamed/removed
    earlier stage leaves a required input artifact unresolvable under
    current names.
    """
    ticket = await get_ticket(ticket_id, org_id=org_id)
    if ticket.current_run_id is None:
        raise RunNotFoundError(ticket_id)
    prior_run = await session.get(PipelineRunRow, ticket.current_run_id)
    if prior_run is None:
        raise RunNotFoundError(ticket.current_run_id)

    org_definitions = await _load_org_definitions(org_id, session=session)
    definition = org_definitions.get(prior_run.pipeline_id) if prior_run.pipeline_id is not None else None
    if definition is None:
        raise StageNotInDefinitionError(from_stage)
    flattened = flatten(definition, org_definitions=org_definitions)

    from_index = next(
        (
            i
            for i, s in enumerate(flattened.stages)
            if isinstance(s, SkillStage | ReviewSkillStage) and s.name == from_stage
        ),
        None,
    )
    if from_index is None:
        raise StageNotInDefinitionError(from_stage)

    await _nearest_upstream_skill_stage_resolvable(
        flattened, org_id=org_id, ticket_id=ticket_id, from_index=from_index, session=session
    )

    own_prior = await latest_final(org_id=org_id, ticket_id=ticket_id, stage_name=from_stage, session=session)
    kickoff = Kickoff(
        intake_point_id="rerun",
        actor=actor,
        input_text=None,
        revision=RevisionContext(
            source="instruction",
            text=instruction,
            prior_artifact=own_prior.body if own_prior is not None else "",
        ),
    )

    row = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=prior_run.pipeline_id,
        pipeline_name=definition.name,
        definition_snapshot={"stages": [stage.model_dump(mode="json") for stage in flattened.stages]},
        state="queued",
        phase="stages",
        current_stage_index=from_index,
        kickoff=kickoff.model_dump(mode="json"),
    )
    session.add(row)
    await session.flush()
    await engine.attempt_promotion(row, session=session)
    return row.id


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
    # `run.state == "paused"` — cancel immediately. A paused run always has
    # exactly one open pause row (created atomically with the state flip).
    pause = await engine.get_open_pause_for_run(run.id, session=session)
    assert pause is not None, f"run {run.id} is paused but has no open run_pauses row"
    await engine.cancel_paused(run, pause, session=session)


class _PauseResolvedPayload(BaseModel):
    action: str


async def resolve_pause(
    pause_id: UUID,
    *,
    resolution: PauseResolution,
    actor: Actor,
    session: AsyncSession,
) -> None:
    """The single HITL resolution entry. `approve` resumes at the next
    boundary; `instruct` re-runs the paused stage fresh with the human's
    text as revision; `send_back` rewinds to an earlier stage (validated
    here, same loop-protected machinery as an automatic send-back); `kill`
    terminates the run.

    Raises `InvalidPauseResolutionError` for a resolution missing its
    required field (`instruct` without `instruction`, `send_back` without
    `send_back_to_stage`) or a `send_back_to_stage` that isn't an upstream
    `SkillStage` in the paused stage's own flattened definition.
    """
    org_id = require_org_context()
    pause = await session.get(RunPauseRow, pause_id)
    if pause is None or pause.org_id != org_id:
        raise PauseNotFoundError(pause_id)
    if actor.user_id is None or not await is_pause_responder(
        actor.user_id, pause.escalation_user_ids, org_id=org_id, session=session
    ):
        raise NotEscalationTargetError(str(pause_id))
    if pause.resolved_at is not None:
        raise PauseAlreadyResolvedError(pause_id)

    if resolution.action == "instruct" and not resolution.instruction:
        raise InvalidPauseResolutionError("instruct requires a non-empty instruction")

    send_back_target: tuple[int, SkillStage] | None = None
    stage_exec: StageExecutionRow | None = None
    if resolution.action == "send_back":
        if not resolution.send_back_to_stage:
            raise InvalidPauseResolutionError("send_back requires send_back_to_stage")
        stage_exec = await session.get(StageExecutionRow, pause.stage_execution_id)
        assert stage_exec is not None
        assert stage_exec.stage_index is not None
        run_for_definition = await session.get(PipelineRunRow, pause.run_id)
        assert run_for_definition is not None
        flattened = FlattenedDefinition.from_snapshot(run_for_definition.definition_snapshot)
        paused_stage = flattened.stages[stage_exec.stage_index]
        assert isinstance(paused_stage, SkillStage | ReviewSkillStage)
        send_back_target = engine.resolve_send_back_target(
            flattened,
            stage=paused_stage,
            before_index=stage_exec.stage_index,
            target_name=resolution.send_back_to_stage,
        )
        if send_back_target is None:
            raise InvalidPauseResolutionError(
                f"send_back_to_stage {resolution.send_back_to_stage!r} is not a valid upstream stage"
            )

    run = await session.get(PipelineRunRow, pause.run_id)
    if run is None:
        raise RunNotFoundError(pause.run_id)

    pause.resolution = resolution.action
    pause.instruction = resolution.instruction
    pause.send_back_to_stage = resolution.send_back_to_stage
    pause.resolved_by = actor.user_id
    pause.resolved_at = datetime.now(UTC)

    await audit(
        "pipeline_run",
        run.id,
        "run.pause_resolved",
        _PauseResolvedPayload(action=resolution.action),
        actor=actor,
        org_id=org_id,
        session=session,
    )

    if resolution.action == "approve":
        await engine.resume_from_pause(pause, run, session=session)
    elif resolution.action == "instruct":
        assert resolution.instruction is not None
        await engine.resume_with_instruction(pause, run, instruction=resolution.instruction, session=session)
    elif resolution.action == "send_back":
        assert send_back_target is not None
        assert stage_exec is not None
        target_index, target_stage = send_back_target
        await engine.resume_with_send_back(
            pause,
            run,
            stage_exec=stage_exec,
            target_index=target_index,
            target_stage=target_stage,
            session=session,
        )
    else:
        assert resolution.action == "kill"
        await engine.kill_run(run, session=session)


async def get_run_state_for_pause(pause_id: UUID, *, session: AsyncSession) -> str:
    """Read-only helper for the `respond` endpoint's `{run_state}` response
    — the owning run's state after `resolve_pause` applied its effect."""
    return (
        await session.execute(
            select(PipelineRunRow.state)
            .join(RunPauseRow, RunPauseRow.run_id == PipelineRunRow.id)
            .where(RunPauseRow.id == pause_id)
        )
    ).scalar_one()


_TEMPLATES_BY_ID: dict[UUID, PipelineDefinition] = {t.id: t for t in defaults.ALL_DEFAULTS}


async def _materialize_template(
    template: PipelineDefinition,
    *,
    org_id: UUID,
    org_by_name: dict[str, UUID],
    memo: dict[UUID, UUID],
    actor: Actor,
    session: AsyncSession,
) -> UUID:
    """Depth-first: a template's own `PipelineCallStage` targets are resolved
    (recursively) before the template itself is created, so the fresh copy's
    call stages can be rewired immediately to a real org pipeline id. A
    callee already present in the org (matched by name) is reused rather
    than copied again; `memo` additionally dedupes within one
    `instantiate_template` call when the same callee is reached more than
    once. The top-level requested template is always freshly copied — the
    by-name reuse rule applies only to callees encountered via
    `PipelineCallStage`."""
    if template.id in memo:
        return memo[template.id]

    new_stages: list[Stage] = []
    for stage in template.stages:
        if isinstance(stage, PipelineCallStage):
            callee = _TEMPLATES_BY_ID.get(stage.pipeline_id)
            assert callee is not None, f"shipped default references unknown template {stage.pipeline_id}"
            org_pipeline_id = org_by_name.get(callee.name)
            if org_pipeline_id is None:
                org_pipeline_id = await _materialize_template(
                    callee, org_id=org_id, org_by_name=org_by_name, memo=memo, actor=actor, session=session
                )
            new_stages.append(stage.model_copy(update={"id": uuid7(), "pipeline_id": org_pipeline_id}))
        else:
            new_stages.append(stage.model_copy(update={"id": uuid7()}))

    fresh = template.model_copy(update={"id": uuid7(), "stages": tuple(new_stages)})
    new_id = await create_pipeline(org_id=org_id, definition=fresh, actor=actor, session=session)
    memo[template.id] = new_id
    org_by_name[template.name] = new_id
    return new_id


async def instantiate_template(
    *, org_id: UUID, template_id: UUID, actor: Actor, session: AsyncSession
) -> UUID:
    """Deep copy of a shipped default: fresh pipeline + stage ids, call
    targets rewired depth-first (a callee already present in the org by name
    is reused, otherwise it's copied too, recursively). Raises
    `TemplateNotFoundError` for an unknown `template_id`."""
    template = _TEMPLATES_BY_ID.get(template_id)
    if template is None:
        raise TemplateNotFoundError(template_id)
    org_definitions = await _load_org_definitions(org_id, session=session)
    org_by_name = {defn.name: pid for pid, defn in org_definitions.items()}
    return await _materialize_template(
        template, org_id=org_id, org_by_name=org_by_name, memo={}, actor=actor, session=session
    )


def list_templates() -> tuple[PipelineDefinition, ...]:
    """The code-shipped default pipelines (pinned ids)."""
    return defaults.ALL_DEFAULTS


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
