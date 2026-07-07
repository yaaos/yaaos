"""Service test: automatic send-back — a residual finding's
`defect_in_artifact` (or a main skill's own `SkillReturn.send_back`) rewinds
the run to an earlier stage, loop-protected against sending back to the
same target twice in one run.

Acceptance flow: a two-stage pipeline (`requirements` → `implement`,
`implement` carries a one-iteration review loop). `implement`'s review
reports a residual finding with `defect_in_artifact="requirements"`; the
loop stops immediately (`max_iterations=1`) so the residual reaches boundary
settlement, which sends back to `requirements` — a NEW `stage_executions`
row at `requirements`' index, carrying `RevisionContext(source="send_back",
text=<the finding's body>, prior_artifact=<requirements' own prior final
artifact>)`. `requirements` re-completes and the run re-runs FORWARD through
`implement` again. A second send-back to `requirements` this run pauses
instead of rewinding again (`tripped={"sendback_loop": "requirements"}`).

Also covers: a main skill's own `SkillReturn.outcome="send_back"` with an
unresolvable `send_back_to_stage` fails the stage (and the run) loudly.

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
from app.core.auth import Role, org_context
from app.core.identity import create_user
from app.core.tenancy import create_membership, create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.pipelines import (
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    ReviewConfig,
    SkillStage,
    create_pipeline,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REQUIREMENTS = "requirements"
_IMPLEMENT = "implement"


async def _seed_org_ticket_and_user(db_session) -> tuple[UUID, UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    user = await create_user(db_session, display_name="Watcher")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="watcher"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="send-back test ticket",
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
                review=ReviewConfig(skill_name="review-implement", max_iterations=1, finding_prefix="SPEC"),
                boundary=BoundaryControl(mode="always_proceed"),
            ),
        ),
    )


def _single_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
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


async def _start(db_session, *, definition: PipelineDefinition) -> tuple[UUID, UUID, UUID, UUID]:
    """Drive `start_run` through provisioning. Returns `(org_id, ticket_id,
    run_id, first_command_id)` with the run parked at stage 0's dispatch."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        agent_row = await seed_agent(org_id=org_id)

        pipeline_id = await create_pipeline(
            org_id=org_id, definition=definition, actor=Actor.system(), session=db_session
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
        first_command_id = run.pending_agent_command_id
        assert first_command_id is not None
        return org_id, ticket_id, run_id, first_command_id


def _skill_output(*, confidence: int = 90, paths_affected: list[str] | None = None) -> str:
    return json.dumps(
        {
            "outcome": "completed",
            "confidence": confidence,
            "paths_affected": paths_affected or [],
            "summary": "done",
        }
    )


@pytest.mark.asyncio
async def test_residual_defect_in_artifact_sends_back_and_reruns_forward_service(db_session) -> None:
    org_id, _ticket_id, run_id, requirements_command_id = await _start(
        db_session, definition=_two_stage_definition()
    )

    # Stage 0 (requirements) completes with its first artifact version.
    await _record(
        org_id,
        _success_event(
            requirements_command_id,
            outputs={"stdout": _skill_output(), "exit_code": 0},
            artifact_body="# v1 spec",
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    implement_main_command_id = run.pending_agent_command_id
    assert implement_main_command_id is not None

    # Stage 1 (implement) main completes; review dispatches.
    await _record(
        org_id,
        _success_event(
            implement_main_command_id,
            outputs={"stdout": _skill_output(), "exit_code": 0},
            artifact_body="# impl v1",
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    review_command_id = run.pending_agent_command_id
    assert review_command_id is not None

    # Review reports one residual finding attributing the defect to
    # `requirements` — `max_iterations=1` stops the loop immediately even
    # though the finding stays open, so this reaches boundary settlement.
    review_output = json.dumps(
        {
            "new_findings": [
                {
                    "severity": "blocker",
                    "body": "the spec is missing the auth requirement",
                    "defect_in_artifact": _REQUIREMENTS,
                }
            ],
            "prior_finding_verdicts": [],
            "confidence": 80,
            "summary": "spec gap found",
        }
    )
    await _record(
        org_id,
        _success_event(review_command_id, outputs={"stdout": review_output, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "running"
    assert run.current_stage_index == 0
    assert run.sendback_counts.get(_REQUIREMENTS) == 1

    # `_send_back_to_stage` dispatches the rewind target's fresh invocation
    # in-process (via `_start_stage_impl`, not a re-enqueue) — the same
    # `drain` cycle that delivered the review's terminal event already
    # created and dispatched the NEW requirements stage_executions row.
    stages = await _stage_rows(db_session, run_id)
    implement_row = next(s for s in stages if s.stage_name == _IMPLEMENT)
    assert implement_row.boundary_outcome == "sent_back"
    assert implement_row.status == "completed"

    requirements_rows = [s for s in stages if s.stage_name == _REQUIREMENTS]
    assert len(requirements_rows) == 2
    assert requirements_rows[0].revision is None
    new_requirements_row = requirements_rows[1]
    rewind_command_id = run.pending_agent_command_id
    assert rewind_command_id is not None
    assert new_requirements_row.revision is not None
    assert new_requirements_row.revision["source"] == "send_back"
    assert new_requirements_row.revision["text"] == "the spec is missing the auth requirement"
    assert new_requirements_row.revision["prior_artifact"] == "# v1 spec"

    # requirements re-completes; the run re-runs FORWARD through implement again.
    await _record(
        org_id,
        _success_event(
            rewind_command_id, outputs={"stdout": _skill_output(), "exit_code": 0}, artifact_body="# v2 spec"
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.current_stage_index == 1
    implement2_command_id = run.pending_agent_command_id
    assert implement2_command_id is not None

    await _record(
        org_id,
        _success_event(
            implement2_command_id,
            outputs={"stdout": _skill_output(), "exit_code": 0},
            artifact_body="# impl v2",
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    review2_command_id = run.pending_agent_command_id
    assert review2_command_id is not None

    # Review reports the SAME defect again — a second send-back to
    # `requirements` this run pauses instead of rewinding.
    await _record(
        org_id,
        _success_event(review2_command_id, outputs={"stdout": review_output, "exit_code": 0}),
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
    assert pause.tripped == {"sendback_loop": _REQUIREMENTS}


@pytest.mark.asyncio
async def test_send_back_to_unresolvable_target_fails_stage_and_run_service(db_session) -> None:
    org_id, _ticket_id, run_id, command_id = await _start(db_session, definition=_single_stage_definition())

    send_back_output = json.dumps(
        {
            "outcome": "send_back",
            "outcome_reason": "cannot proceed without an upstream stage that does not exist",
            "send_back_to_stage": "does-not-exist",
            "confidence": 50,
            "paths_affected": [],
            "summary": "blocked",
        }
    )
    await _record(
        org_id,
        _success_event(command_id, outputs={"stdout": send_back_output, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    stages = await _stage_rows(db_session, run_id)
    implement_row = next(s for s in stages if s.stage_name == _IMPLEMENT)
    assert implement_row.status == "failed"

    # The stage failure routes through cleanup (a workspace was provisioned)
    # before the run itself reaches its terminal `failed` state.
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
    assert run.state == "failed"
    assert run.failure_reason is not None
    assert "does-not-exist" in run.failure_reason
