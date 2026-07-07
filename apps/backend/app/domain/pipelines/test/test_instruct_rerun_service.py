"""Service test: `resolve_pause(instruct)`, `start_rerun_from_stage`, and
the Runs/Overview read models.

Acceptance flow:
- `resolve_pause(instruct)` on a paused run creates a NEW `stage_executions`
  row at the SAME `stage_index` carrying `revision(source="instruction")`
  with the human's text — the same run continues (not a new run).
- `start_rerun_from_stage` on a completed run starts a NEW run on the
  current definition, beginning at `from_stage`'s index; the earlier
  stage's artifact is read through (not copied) — the new run's FIRST
  dispatch carries `revision(source="instruction")` with `prior_artifact`
  pulled from the original run's own final artifact.
- `from_stage` unresolvable in the current definition raises
  `StageNotInDefinitionError`.
- `views.list_runs_for_ticket` / `views.get_run_overview` shapes.

Uses the shared `drain` outbox-dispatch helper (`test/drain.py`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.core.agent_gateway import AgentEvent, AgentEventKind, record_agent_event
from app.core.agent_gateway import Artifact as WireArtifact
from app.core.audit_log import Actor, ActorKind
from app.core.auth import Role, org_context, user_id_var
from app.core.identity import create_user
from app.core.tenancy import create_membership, create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.pipelines import (
    BoundaryControl,
    Kickoff,
    PauseResolution,
    PipelineDefinition,
    SkillStage,
    StageNotInDefinitionError,
    create_pipeline,
    resolve_pause,
    start_rerun_from_stage,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.pipelines.test.drain import drain
from app.domain.pipelines.views import get_run_overview, list_runs_for_ticket
from app.domain.tickets import create_from_pr
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REQUIREMENTS = "requirements"
_IMPLEMENT = "implement"


async def _seed_org_ticket_and_user(db_session) -> tuple[UUID, UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    user = await create_user(db_session, display_name="Requester")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="requester"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="instruct/rerun test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.execute(
        text("UPDATE tickets SET branch_name = :branch WHERE id = :id"),
        {"branch": "yaaos/test-branch", "id": ticket_id},
    )
    await db_session.flush()
    return org.org_id, ticket_id, user.id


def _one_skill_stage_definition(*, boundary: BoundaryControl) -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_IMPLEMENT,
                skill_name=_IMPLEMENT,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=boundary,
            ),
        ),
    )


def _two_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_REQUIREMENTS,
                skill_name=_REQUIREMENTS,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
            SkillStage(
                name=_IMPLEMENT,
                skill_name=_IMPLEMENT,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
        ),
    )


def _success_event(command_id: UUID, *, outputs: dict, artifact_body: str | None = None) -> AgentEvent:
    return AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs=outputs,
        reported_at=datetime.now(UTC),
        traceparent="",
        artifact=WireArtifact(body=artifact_body) if artifact_body is not None else None,
    )


async def _record(org_id: UUID, event: AgentEvent, *, agent_id: UUID | None, db_session) -> None:
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()


def _skill_output(*, confidence: int = 90) -> str:
    return json.dumps(
        {"outcome": "completed", "confidence": confidence, "paths_affected": [], "summary": "ok"}
    )


async def _stage_rows(db_session, run_id: UUID) -> list[StageExecutionRow]:
    return (
        (
            await db_session.execute(
                select(StageExecutionRow)
                .where(StageExecutionRow.run_id == run_id)
                .order_by(StageExecutionRow.started_at)
            )
        )
        .scalars()
        .all()
    )


async def _finish_via_cleanup(org_id: UUID, run_id: UUID, db_session) -> PipelineRunRow:
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "cleanup"
    cleanup_command_id = run.pending_agent_command_id
    assert cleanup_command_id is not None
    await _record(
        org_id, _success_event(cleanup_command_id, outputs={}), agent_id=None, db_session=db_session
    )
    await drain(db_session)
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    return run


@pytest.mark.asyncio
async def test_instruct_on_paused_run_continues_same_run_service(db_session) -> None:
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        agent_row = await seed_agent(org_id=org_id)
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_one_skill_stage_definition(boundary=BoundaryControl(mode="always_hitl")),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text="build it")
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_command_id = run.pending_agent_command_id
        assert provision_command_id is not None
        await _record(
            org_id,
            _success_event(provision_command_id, outputs={}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        skill_command_id = run.pending_agent_command_id
        assert skill_command_id is not None
        await _record(
            org_id,
            _success_event(
                skill_command_id, outputs={"stdout": _skill_output(), "exit_code": 0}, artifact_body="# v1"
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "paused"

        pause = (
            await db_session.execute(
                select(RunPauseRow).where(RunPauseRow.run_id == run_id, RunPauseRow.resolved_at.is_(None))
            )
        ).scalar_one()

        async with org_context(org_id, ActorKind.SYSTEM):
            await resolve_pause(
                pause.id,
                resolution=PauseResolution(action="instruct", instruction="add a retry"),
                actor=Actor.user(user_id=user_id),
                session=db_session,
            )
        await db_session.commit()

        # Same run — no new pipeline_runs row.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "running"

        pause_row = await db_session.get(RunPauseRow, pause.id)
        assert pause_row is not None
        assert pause_row.resolution == "instruct"
        assert pause_row.resolved_at is not None

        stages = await _stage_rows(db_session, run_id)
        implement_rows = [s for s in stages if s.stage_name == _IMPLEMENT]
        assert len(implement_rows) == 2
        assert implement_rows[0].revision is None
        instructed_row = implement_rows[1]
        assert instructed_row.stage_index == implement_rows[0].stage_index
        assert instructed_row.revision is not None
        assert instructed_row.revision["source"] == "instruction"
        assert instructed_row.revision["text"] == "add a retry"
        assert instructed_row.revision["prior_artifact"] == "# v1"
        assert run.pending_agent_command_id is not None


@pytest.mark.asyncio
async def test_rerun_from_stage_on_completed_run_starts_new_run_inheriting_artifacts_service(
    db_session,
) -> None:
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        agent_row = await seed_agent(org_id=org_id)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_two_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text="build it")
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_command_id = run.pending_agent_command_id
        assert provision_command_id is not None
        await _record(
            org_id,
            _success_event(provision_command_id, outputs={}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        requirements_command_id = run.pending_agent_command_id
        assert requirements_command_id is not None
        await _record(
            org_id,
            _success_event(
                requirements_command_id,
                outputs={"stdout": _skill_output(), "exit_code": 0},
                artifact_body="# spec v1",
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        implement_command_id = run.pending_agent_command_id
        assert implement_command_id is not None
        await _record(
            org_id,
            _success_event(
                implement_command_id,
                outputs={"stdout": _skill_output(), "exit_code": 0},
                artifact_body="# impl v1",
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await _finish_via_cleanup(org_id, run_id, db_session)
        assert run.state == "completed"

        # `from_stage` unresolvable in the current definition — 409-mapped.
        with pytest.raises(StageNotInDefinitionError):
            async with org_context(org_id, ActorKind.SYSTEM):
                await start_rerun_from_stage(
                    org_id=org_id,
                    ticket_id=ticket_id,
                    from_stage="does-not-exist",
                    instruction="redo it",
                    actor=Actor.user(user_id=user_id),
                    session=db_session,
                )

        async with org_context(org_id, ActorKind.SYSTEM):
            rerun_id = await start_rerun_from_stage(
                org_id=org_id,
                ticket_id=ticket_id,
                from_stage=_IMPLEMENT,
                instruction="redo the impl with better error handling",
                actor=Actor.user(user_id=user_id),
                session=db_session,
            )
        await db_session.commit()

        assert rerun_id != run_id
        rerun = await db_session.get(PipelineRunRow, rerun_id)
        assert rerun is not None
        assert rerun.ticket_id == ticket_id
        assert rerun.current_stage_index == 1  # implement's index
        assert rerun.state in ("queued", "running")

        await drain(db_session)

        rerun = await db_session.get(PipelineRunRow, rerun_id)
        assert rerun is not None
        assert rerun.phase == "provision"
        rerun_provision_command_id = rerun.pending_agent_command_id
        assert rerun_provision_command_id is not None
        await _record(
            org_id,
            _success_event(rerun_provision_command_id, outputs={}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        # No `requirements` stage_execution on the new run — its artifact is
        # read through from the FIRST run, not re-produced. Only the system
        # provision row plus `implement` itself.
        rerun_stages = await _stage_rows(db_session, rerun_id)
        assert [s.stage_name for s in rerun_stages] == ["provision-workspace", _IMPLEMENT]
        implement_row = next(s for s in rerun_stages if s.stage_name == _IMPLEMENT)
        assert implement_row.stage_index == 1
        assert implement_row.revision is not None
        assert implement_row.revision["source"] == "instruction"
        assert implement_row.revision["text"] == "redo the impl with better error handling"
        assert implement_row.revision["prior_artifact"] == "# impl v1"


@pytest.mark.asyncio
async def test_run_and_overview_shapes_service(db_session) -> None:
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_one_skill_stage_definition(boundary=BoundaryControl(mode="always_hitl")),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()
        agent_row = await seed_agent(org_id=org_id)

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text="build it")
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_command_id = run.pending_agent_command_id
        assert provision_command_id is not None
        await _record(
            org_id,
            _success_event(provision_command_id, outputs={}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        async with org_context(org_id, ActorKind.SYSTEM):
            in_flight_runs = await list_runs_for_ticket(ticket_id, session=db_session)
        assert len(in_flight_runs) == 1
        assert in_flight_runs[0].id == run_id
        assert in_flight_runs[0].kickoff.actor_kind == "user"
        assert any(s.stage_name == "provision-workspace" for s in in_flight_runs[0].stages)

        token = user_id_var.set(user_id)
        try:
            async with org_context(org_id, ActorKind.SYSTEM):
                in_flight_overview = await get_run_overview(ticket_id, session=db_session)
        finally:
            user_id_var.reset(token)
        assert in_flight_overview is not None
        assert in_flight_overview.status == "in_flight"
        assert in_flight_overview.run is not None
        assert in_flight_overview.run.id == run_id

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        skill_command_id = run.pending_agent_command_id
        assert skill_command_id is not None
        await _record(
            org_id,
            _success_event(
                skill_command_id, outputs={"stdout": _skill_output(), "exit_code": 0}, artifact_body="# v1"
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "paused"

        token = user_id_var.set(user_id)
        try:
            async with org_context(org_id, ActorKind.SYSTEM):
                paused_overview = await get_run_overview(ticket_id, session=db_session)
        finally:
            user_id_var.reset(token)
        assert paused_overview is not None
        assert paused_overview.status == "paused"
        assert paused_overview.pause is not None
        assert paused_overview.pause.stage_name == _IMPLEMENT
        assert paused_overview.pause.can_respond is True
        assert paused_overview.pause.escalation_logins == ("requester",)
