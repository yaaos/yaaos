"""Service test: the `core/agent_gateway` agent-event consumer registry —
`record_agent_event` enqueues `domain/pipelines.handle_agent_event` for
every terminal event; an event whose `workflow_execution_id` doesn't
correspond to a `pipeline_runs` row is a safe no-op (not an error), and the
matching run resumes off its own terminal event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from sqlalchemy import text

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    CleanupWorkspaceCommand,
    enqueue_command,
    record_agent_event,
)
from app.core.audit_log import Actor, ActorKind
from app.core.auth import Role, org_context
from app.core.identity import create_user
from app.core.tenancy import create_membership, create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.pipelines import (
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    SkillStage,
    create_pipeline,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]


def _one_skill_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name="write-spec",
                skill_name="write-spec",
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(),
            ),
        ),
    )


async def _start_pipelines_flow(db_session) -> tuple[UUID, UUID, UUID]:
    """Returns `(org_id, run_id, provision_command_id)`, parked in `phase='provision'`."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    org = await create_org(db_session, slug=f"seam-{uuid4().hex[:8]}", display_name="Seam Org")
    user = await create_user(db_session, display_name="Seam User")
    await create_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="seam")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="seam test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.execute(
        text("UPDATE tickets SET branch_name = :branch WHERE id = :id"),
        {"branch": "yaaos/seam-branch", "id": ticket_id},
    )
    await db_session.flush()

    with register_stub_vcs(plugin_id="github"):
        pipeline_id = await create_pipeline(
            org_id=org.org_id,
            definition=_one_skill_stage_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user.id), input_text="go")
        run_id = await start_run(
            org_id=org.org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "provision"
    assert run.pending_agent_command_id is not None
    return org.org_id, run_id, run.pending_agent_command_id


def _success_event(command_id: UUID) -> AgentEvent:
    return AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )


@pytest.mark.asyncio
async def test_foreign_run_id_is_a_no_op_and_the_real_run_still_resumes(db_session) -> None:
    """A terminal event whose `workflow_execution_id` doesn't correspond to
    any `pipeline_runs` row is a no-op that leaves a parked run untouched;
    the parked run's own terminal event still resumes it normally."""
    pipe_org_id, run_id, provision_command_id = await _start_pipelines_flow(db_session)

    # Enqueue a real command stamped with a `workflow_execution_id` that
    # isn't any `pipeline_runs.id` — `handle_agent_event` sees an id it
    # doesn't own and no-ops.
    foreign_command_id = uuid7()
    foreign_run_id = uuid4()
    await enqueue_command(
        org_id=pipe_org_id,
        command=CleanupWorkspaceCommand(command_id=foreign_command_id, workspace_id=uuid4(), traceparent=""),
        session=db_session,
        workflow_execution_id=foreign_run_id,
    )
    await db_session.commit()

    async with org_context(pipe_org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(_success_event(foreign_command_id), session=db_session)
    await db_session.commit()
    await drain(db_session)

    pipe_run = await db_session.get(PipelineRunRow, run_id)
    assert pipe_run is not None
    assert pipe_run.phase == "provision"  # untouched by the foreign event
    assert pipe_run.pending_agent_command_id == provision_command_id

    # The run's own terminal event resumes it.
    async with org_context(pipe_org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(_success_event(provision_command_id), session=db_session)
    await db_session.commit()
    await drain(db_session)

    pipe_run = await db_session.get(PipelineRunRow, run_id)
    assert pipe_run is not None
    assert pipe_run.phase == "stages"  # resumed
