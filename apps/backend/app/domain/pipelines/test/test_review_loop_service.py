"""Service test: the review-fix loop with durable finding lifecycle.

Acceptance flow: a one-skill-stage pipeline with a review loop drives
`main -> review -> fix -> review`. Iteration-1 findings materialize `open`
with handles `<prefix>-001…`; a `fixed` verdict on iteration 2 flips one to
`resolved` with a `review_verdict` status event; the fix invocation's
`revision` is durably recorded on the stage-execution row (`source="fix"`,
text rendered from the residual findings); the loop stops at
`max_iterations` even though a residual remains; residuals land in
`loop_state`.

Also covers: a standalone `kind='review'` stage (`ReviewSkillStage`) —
one invocation, findings materialize, no artifact.

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
from app.domain.findings import list_for_stage_execution
from app.domain.pipelines import (
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    ReviewConfig,
    ReviewSkillStage,
    SkillStage,
    create_pipeline,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_STAGE_NAME = "implement"


async def _seed_org_ticket_and_user(db_session) -> tuple[UUID, UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    user = await create_user(db_session, display_name="Watcher")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="watcher"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="review loop test ticket",
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


def _review_loop_definition() -> PipelineDefinition:
    # `mode="always_proceed"` — this file exercises the review/fix loop, not
    # boundary policy; `BoundaryControl()`'s own default (`always_hitl`)
    # would pause every run here instead of completing it.
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_STAGE_NAME,
                skill_name=_STAGE_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                review=ReviewConfig(skill_name="review-implement", max_iterations=2, finding_prefix="SPEC"),
                boundary=BoundaryControl(mode="always_proceed"),
            ),
        ),
    )


def _review_only_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            ReviewSkillStage(
                name="code-review",
                skill_name="code-review",
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                finding_prefix="REV",
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


async def _advance_to_main_dispatch(
    db_session, *, definition: PipelineDefinition
) -> tuple[UUID, UUID, UUID, UUID]:
    """Drive `start_run` through the provision system stage's terminal event.

    Returns `(org_id, ticket_id, run_id, first_command_id)` with the run
    parked awaiting stage 0's terminal event.
    """
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        agent_row = await seed_agent(org_id=org_id)

        pipeline_id = await create_pipeline(
            org_id=org_id, definition=definition, actor=Actor.system(), session=db_session
        )
        await db_session.flush()

        kickoff = Kickoff(
            intake_point_id="test", actor=Actor.user(user_id=user_id), input_text="implement the feature"
        )
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.phase == "provision"
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
        assert run.phase == "stages"
        first_command_id = run.pending_agent_command_id
        assert first_command_id is not None

        return org_id, ticket_id, run_id, first_command_id


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


async def _stage_rows(db_session, run_id: UUID) -> list[StageExecutionRow]:
    return (
        (
            await db_session.execute(
                select(StageExecutionRow)
                .where(StageExecutionRow.run_id == run_id)
                .order_by(StageExecutionRow.started_at, StageExecutionRow.id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_review_fix_loop_acceptance(db_session) -> None:
    org_id, _ticket_id, run_id, main_command_id = await _advance_to_main_dispatch(
        db_session, definition=_review_loop_definition()
    )

    # Main dispatch completes with an artifact — stage.review is configured,
    # so the engine dispatches a review pass instead of proceeding.
    main_output = json.dumps(
        {"outcome": "completed", "confidence": 80, "paths_affected": [], "summary": "wrote v1"}
    )
    await _record(
        org_id,
        _success_event(
            main_command_id, outputs={"stdout": main_output, "exit_code": 0}, artifact_body="# Draft v1"
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "running"
    review1_command_id = run.pending_agent_command_id
    assert review1_command_id is not None

    stages = await _stage_rows(db_session, run_id)
    skill_row = next(s for s in stages if s.stage_name == _STAGE_NAME)
    assert skill_row.phase == "review"
    assert skill_row.iteration == 1

    # Iteration-1 review reports two findings — no verdicts yet.
    review1_output = json.dumps(
        {
            "new_findings": [
                {"severity": "blocker", "body": "SQL injection risk", "code_file": "app.py", "code_line": 10},
                {"severity": "nit", "body": "naming nit"},
            ],
            "prior_finding_verdicts": [],
            "confidence": 70,
            "summary": "found issues",
        }
    )
    await _record(
        org_id,
        _success_event(review1_command_id, outputs={"stdout": review1_output, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    findings = await list_for_stage_execution(skill_row.id, session=db_session)
    assert sorted(f.handle for f in findings) == ["SPEC-001", "SPEC-002"]
    assert all(f.status == "open" for f in findings)
    blocker_finding = next(f for f in findings if f.severity == "blocker")
    nit_finding = next(f for f in findings if f.severity == "nit")

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    fix_command_id = run.pending_agent_command_id
    assert fix_command_id is not None
    assert fix_command_id != review1_command_id

    stages = await _stage_rows(db_session, run_id)
    skill_row = next(s for s in stages if s.stage_name == _STAGE_NAME)
    assert skill_row.phase == "fix"
    # The fix invocation's revision is durably recorded — proof it received
    # the residual findings, independent of the (stubbed) wire payload.
    assert skill_row.revision is not None
    assert skill_row.revision["source"] == "fix"
    assert blocker_finding.handle in skill_row.revision["text"]
    assert nit_finding.handle in skill_row.revision["text"]

    # Fix produces a new artifact version, then re-reviews.
    fix_output = json.dumps(
        {"outcome": "completed", "confidence": 85, "paths_affected": [], "summary": "fixed the blocker"}
    )
    await _record(
        org_id,
        _success_event(
            fix_command_id, outputs={"stdout": fix_output, "exit_code": 0}, artifact_body="# Draft v2"
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    review2_command_id = run.pending_agent_command_id
    assert review2_command_id is not None
    assert review2_command_id != review1_command_id

    stages = await _stage_rows(db_session, run_id)
    skill_row = next(s for s in stages if s.stage_name == _STAGE_NAME)
    assert skill_row.phase == "review"
    assert skill_row.iteration == 2

    # Iteration-2 review: the blocker is fixed, the nit is untouched.
    review2_output = json.dumps(
        {
            "new_findings": [],
            "prior_finding_verdicts": [{"finding_id": str(blocker_finding.id), "status": "fixed"}],
            "confidence": 90,
            "summary": "blocker resolved",
        }
    )
    await _record(
        org_id,
        _success_event(review2_command_id, outputs={"stdout": review2_output, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    findings_after = await list_for_stage_execution(skill_row.id, session=db_session)
    resolved = next(f for f in findings_after if f.id == blocker_finding.id)
    assert resolved.status == "resolved"
    assert resolved.status_events[-1].status == "resolved"
    assert resolved.status_events[-1].method == "review_verdict"
    still_open_nit = next(f for f in findings_after if f.id == nit_finding.id)
    assert still_open_nit.status == "open"

    # max_iterations=2 hit — the loop stops even though the nit residual
    # remains; the stage's `mode="always_proceed"` boundary control means
    # the residual never trips a pause.
    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "completed"

    stages = await _stage_rows(db_session, run_id)
    skill_row = next(s for s in stages if s.stage_name == _STAGE_NAME)
    assert skill_row.status == "completed"
    review_entries = [e for e in skill_row.loop_state if e["phase"] == "review"]
    assert len(review_entries) == 2
    assert review_entries[-1]["iteration"] == 2
    assert review_entries[-1]["residual_finding_ids"] == [str(nit_finding.id)]


@pytest.mark.asyncio
async def test_review_only_stage_produces_findings_no_artifact(db_session) -> None:
    org_id, ticket_id, run_id, review_command_id = await _advance_to_main_dispatch(
        db_session, definition=_review_only_stage_definition()
    )

    stages = await _stage_rows(db_session, run_id)
    review_row = next(s for s in stages if s.stage_name == "code-review")
    assert review_row.kind == "review"
    assert review_row.phase == "review"

    review_output = json.dumps(
        {
            "new_findings": [{"severity": "should_fix", "body": "missing null check"}],
            "prior_finding_verdicts": [],
            "confidence": 65,
            "summary": "one issue found",
        }
    )
    await _record(
        org_id,
        _success_event(review_command_id, outputs={"stdout": review_output, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    findings = await list_for_stage_execution(review_row.id, session=db_session)
    assert len(findings) == 1
    assert findings[0].handle == "REV-001"
    assert findings[0].status == "open"

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "completed"

    artifact_count = (
        await db_session.execute(
            text("SELECT count(*) FROM artifacts WHERE ticket_id = :ticket_id"), {"ticket_id": ticket_id}
        )
    ).scalar_one()
    assert artifact_count == 0
