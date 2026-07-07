"""Service test: the `core/agent_gateway` agent-event consumer registry —
one `record_agent_event` call reaches both the old engine's and this
engine's registered `HANDLE_AGENT_EVENT`; each ignores a `workflow_execution_id`
it doesn't own; the old engine's flow still resumes through the shared
registry (the coexistence bridge between the two engines).

Drives two independent parked flows in parallel — an old-engine `Workflow`
(cloned from `core/workflow/test/test_workspace_dispatch_service.py`'s
`_DispatchingWs` pattern) and a pipelines run parked on its
`provision-workspace` system stage — then fires each flow's terminal event
in turn, asserting the OTHER flow's row is untouched.
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
from app.core.workflow import (
    AgentDispatchCommand,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_execution_summary,
    step,
)
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
from app.testing.workflow_harness import set_engine_for_tests

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]


# ── Old-engine flow — cloned from test_workspace_dispatch_service.py ────


class _DispatchingWs(AgentDispatchCommand):
    kind = "SeamTestDispatchingWs"
    Inputs = Empty
    Outputs = Empty
    _org_id: UUID = UUID("00000000-0000-0000-0000-000000000000")
    dispatched_command_id: UUID | None = None

    async def execute(self, inputs: Empty, ctx) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs: Empty, ctx, *, session) -> UUID:  # type: ignore[no-untyped-def]
        del inputs
        command_id = uuid7()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id, workspace_id=uuid4(), traceparent=ctx.traceparent or ""
        )
        await enqueue_command(
            org_id=type(self)._org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        type(self).dispatched_command_id = command_id
        return command_id


class _NoopLocal:
    kind = "SeamTestDispatchingWsTerminal"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx, *, session=None) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        return Outcome.success()


async def _start_old_engine_flow(eng, db_session) -> tuple[str, UUID]:
    """Returns `(workflow_execution_id, dispatched_command_id)`, parked AWAITING_AGENT."""
    org_id = uuid4()
    _DispatchingWs._org_id = org_id
    _DispatchingWs.dispatched_command_id = None

    dispatch_step = step(_DispatchingWs)
    terminal_step = step(_NoopLocal)
    workflow = Workflow(
        name="event-consumer-seam-test",
        version=1,
        steps=(dispatch_step, terminal_step),
        entry=dispatch_step,
        transitions={
            dispatch_step: {"success": terminal_step},
            terminal_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
        },
    )
    eng.register_workflow(workflow)
    wfx_id = await eng.start(
        workflow_name="event-consumer-seam-test", ticket_id=str(uuid4()), session=db_session
    )
    await db_session.commit()
    await drain(db_session)

    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx is not None
    assert wfx.state == WorkflowState.AWAITING_AGENT.value
    assert _DispatchingWs.dispatched_command_id is not None
    return wfx_id, _DispatchingWs.dispatched_command_id


# ── Pipelines-engine flow — parked on its provision system stage ───────


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
async def test_one_event_reaches_both_consumers_and_each_ignores_foreign_ids(db_session) -> None:
    with set_engine_for_tests() as eng:
        old_wfx_id, old_command_id = await _start_old_engine_flow(eng, db_session)
        pipe_org_id, run_id, provision_command_id = await _start_pipelines_flow(db_session)

        # Fire the OLD engine's terminal event. Both consumers receive it —
        # the pipelines consumer must ignore `old_wfx_id` (not a pipeline_runs
        # id) and leave the still-parked pipelines run untouched.
        async with org_context(uuid4(), ActorKind.WORKSPACE, actor_id=None):
            await record_agent_event(_success_event(old_command_id), session=db_session)
        await db_session.commit()
        await drain(db_session)

        old_wfx = await get_execution_summary(UUID(old_wfx_id), session=db_session)
        assert old_wfx is not None
        assert old_wfx.state == WorkflowState.DONE.value  # old engine resumed to completion

        pipe_run = await db_session.get(PipelineRunRow, run_id)
        assert pipe_run is not None
        assert pipe_run.phase == "provision"  # untouched by the foreign event
        assert pipe_run.pending_agent_command_id == provision_command_id

        # Fire the PIPELINES engine's terminal event. Both consumers receive
        # it — the old engine's consumer must ignore `run_id` (not a
        # WorkflowExecutionRow id) and leave the already-DONE row untouched.
        async with org_context(pipe_org_id, ActorKind.WORKSPACE, actor_id=None):
            await record_agent_event(_success_event(provision_command_id), session=db_session)
        await db_session.commit()
        await drain(db_session)

        pipe_run = await db_session.get(PipelineRunRow, run_id)
        assert pipe_run is not None
        assert pipe_run.phase == "stages"  # pipelines engine resumed

        old_wfx = await get_execution_summary(UUID(old_wfx_id), session=db_session)
        assert old_wfx is not None
        assert old_wfx.state == WorkflowState.DONE.value  # unaffected by the foreign event
