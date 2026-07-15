"""Service tests: stage adoption — plain-forward-dispatch adoption fork.

Acceptance: a service test driving the shipped `dev` pipeline over a ticket
with a `pipeline-requirements`-produced attachment observes zero main-skill
coding-agent invocations for the requirements stage, an artifact row with
`adopted_from_attachment_id` set, the stage's review dispatch firing, and
`stage_executions.confidence` stamped from the review return.

Covers:
(a) Adopt happy path: no main invocation, artifact stored+final with
    provenance, review dispatched, loop_state main entry adopted:true.
(b) Context-only attachment (no produced_by_skill) → no adoption, main runs.
(c) Precedence: newer attachment beats older engine artifact.
(d) Re-kickoff WITHOUT re-attach → run-1's adopted artifact is newer than
    the attachment → main runs for real on the second run.
(e) Review return stamps confidence iff stage_exec.confidence is NULL (adopted
    stages); engine-produced stages keep main self-report.
(f) Adopted stage with review off → boundary settles with NULL confidence;
    on_confidence_below doesn't trip.
(g) Fix-loop: review blocker on adopted artifact dispatches main with residuals.
(h) Skill-version mismatch: attachment with an older skill_version logs a warning
    but adoption still proceeds normally.
(i) Skill-version match: no warning logged when versions agree.

Uses the shared `drain` outbox-dispatch helper (`test/drain.py`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.agent_gateway import AgentEvent, AgentEventKind, record_agent_event
from app.core.agent_gateway import Artifact as WireArtifact
from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.tenancy import create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.attachments import add_attachment
from app.domain.findings import list_for_stage_execution
from app.domain.pipelines import (
    BoundaryControl,
    PipelineDefinition,
    ReviewConfig,
    SkillStage,
    create_pipeline,
)
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.service import start_manual_run
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_manual
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

# Skill name must match what the attachment's frontmatter declares.
_SKILL_NAME = "pipeline-requirements"
_STAGE_NAME = "requirements"
_REVIEW_SKILL = "pipeline-requirements-review"

# Minimal valid frontmatter block for an adopted attachment.
_FRONTMATTER_BODY = (
    "---\n"
    "yaaos_artifact_version: 1\n"
    f"skill: {_SKILL_NAME}\n"
    "skill_version: '1.0.0'\n"
    "artifact_type: requirements\n"
    "produced_at: '2026-01-01T00:00:00Z'\n"
    "---\n\n"
    "# Requirements\n\n"
    "Feature: login page\n"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _seed_org_and_ticket(db_session: AsyncSession) -> tuple[UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_manual(
        org_id=org.org_id,
        title="adoption test ticket",
        repo_external_id="acme/repo",
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.execute(
        text("UPDATE tickets SET branch_name = :b WHERE id = :id"),
        {"b": "yaaos/test-branch", "id": ticket_id},
    )
    await db_session.flush()
    return org.org_id, ticket_id


def _adoption_stage_definition(
    *, with_review: bool = True, boundary_mode: str = "always_proceed"
) -> PipelineDefinition:
    """One-stage pipeline whose skill name matches `_SKILL_NAME`."""
    review = ReviewConfig(skill_name=_REVIEW_SKILL, max_iterations=1) if with_review else None
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_STAGE_NAME,
                skill_name=_SKILL_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                review=review,
                boundary=BoundaryControl(mode=boundary_mode),
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


async def _record(
    org_id: UUID, event: AgentEvent, *, agent_id: UUID | None, db_session: AsyncSession
) -> None:
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()


def _review_return_json(*, confidence: int = 85, findings: list | None = None) -> str:
    return json.dumps(
        {
            "confidence": confidence,
            "new_findings": findings or [],
            "prior_finding_verdicts": [],
            "summary": "review complete",
        }
    )


async def _finish_via_cleanup(org_id: UUID, run_id: UUID, db_session: AsyncSession) -> PipelineRunRow:
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "cleanup"
    cleanup_cmd = run.pending_agent_command_id
    assert cleanup_cmd is not None
    await _record(org_id, _success_event(cleanup_cmd, outputs={}), agent_id=None, db_session=db_session)
    await drain(db_session)
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    return run


# ---------------------------------------------------------------------------
# Test (a): adopt happy path — no main invocation, review dispatched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_happy_path_no_main_invocation(db_session: AsyncSession) -> None:
    """When a matching attachment is present and newer than any prior artifact,
    the engine synthesises the main phase (stores+finalises the attachment body
    as the artifact) and dispatches the review invocation without ever
    dispatching a main-skill coding-agent command.

    Assert:
    - No main `InvokeClaudeCode` command dispatched (run is parked on review).
    - Artifact row exists with `adopted_from_attachment_id = attachment.id`.
    - `stage_executions.loop_state[0]` has `adopted=true, confidence=null`.
    - `stage_executions.phase = "review"` (parked waiting for review result).
    """
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        # Add attachment with valid frontmatter matching _SKILL_NAME.
        att = await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=True),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_cmd = run.pending_agent_command_id
        assert provision_cmd is not None

        await _record(
            org_id, _success_event(provision_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs runs (attachment present).
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        assert seed_cmd is not None
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # After seed-inputs + adoption, the run should be parked on the REVIEW
        # command, not a main-skill command.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        review_cmd = run.pending_agent_command_id
        assert review_cmd is not None

        # Verify via the stage_exec row that we're in "review" phase.
        stage_rows = (
            (
                await db_session.execute(
                    select(StageExecutionRow).where(
                        StageExecutionRow.run_id == run_id, StageExecutionRow.kind == "skill"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stage_rows) == 1
        stage_exec = stage_rows[0]
        assert stage_exec.phase == "review", "should be parked on review, not main"
        assert stage_exec.status == "running"

        # loop_state has one "main" entry with adopted=true.
        assert len(stage_exec.loop_state) == 1
        main_entry = stage_exec.loop_state[0]
        assert main_entry["phase"] == "main"
        assert main_entry.get("adopted") is True
        assert main_entry.get("confidence") is None  # no main-skill confidence

        # stage_exec.confidence is NULL (will be filled by review return).
        assert stage_exec.confidence is None

        # Artifact row exists with adopted_from_attachment_id set.
        artifact_id = UUID(main_entry["artifact_id"])
        artifact_row = (
            await db_session.execute(
                text("SELECT adopted_from_attachment_id, is_final, body FROM artifacts WHERE id = :id"),
                {"id": artifact_id},
            )
        ).one()
        assert artifact_row.adopted_from_attachment_id == att.id
        assert artifact_row.is_final is True
        assert artifact_row.body == _FRONTMATTER_BODY


# ---------------------------------------------------------------------------
# Test (b): context-only attachment → no adoption, main skill runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_only_attachment_no_adoption(db_session: AsyncSession) -> None:
    """An attachment with no frontmatter (produced_by_skill is NULL) is a
    context-only attachment and is never matched for adoption.  The main
    skill dispatch runs as normal."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        # Attachment with NO frontmatter → produced_by_skill=None → context-only.
        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="notes.md",
            body="# Notes\nsome notes without frontmatter",
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=False),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_cmd = run.pending_agent_command_id
        assert provision_cmd is not None

        await _record(
            org_id, _success_event(provision_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs runs (attachment present), then skill stage dispatches.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        assert seed_cmd is not None
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        skill_cmd = run.pending_agent_command_id
        assert skill_cmd is not None

        # Main skill InvokeClaudeCode was dispatched (no adoption).
        cmd_row = (
            await db_session.execute(
                text("SELECT command_kind FROM agent_commands WHERE id = :id"),
                {"id": skill_cmd},
            )
        ).one()
        assert cmd_row.command_kind == "InvokeClaudeCode"

        # stage_exec phase is "main" (not "review").
        stage_rows = (
            (
                await db_session.execute(
                    select(StageExecutionRow).where(
                        StageExecutionRow.run_id == run_id, StageExecutionRow.kind == "skill"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stage_rows) == 1
        assert stage_rows[0].phase == "main"


# ---------------------------------------------------------------------------
# Test (c): precedence — newer attachment beats older engine artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_precedence_newer_attachment_beats_older_artifact(db_session: AsyncSession) -> None:
    """The attachment is newer (higher attached_at) than the ticket's prior
    final artifact for this stage → adoption wins; the artifact body comes
    from the attachment, NOT from the prior artifact."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        # Seed a prior (stale) artifact for the same stage directly into the DB.
        prior_run_id = uuid4()
        prior_stage_exec_id = uuid4()
        prior_artifact_id = uuid4()
        await db_session.execute(
            text(
                """
                INSERT INTO pipeline_runs (id, org_id, ticket_id, pipeline_id, pipeline_name,
                    definition_snapshot, kickoff, state, phase)
                VALUES (:id, :org_id, :ticket_id, :pid, 'old', '{}', '{}', 'completed', 'stages')
                """
            ),
            {"id": prior_run_id, "org_id": org_id, "ticket_id": ticket_id, "pid": uuid4()},
        )
        await db_session.execute(
            text(
                """
                INSERT INTO stage_executions (id, org_id, run_id, stage_index, kind,
                    stage_name, status, phase)
                VALUES (:id, :org_id, :run_id, 0, 'skill', :stage_name, 'completed', 'main')
                """
            ),
            {"id": prior_stage_exec_id, "org_id": org_id, "run_id": prior_run_id, "stage_name": _STAGE_NAME},
        )
        await db_session.execute(
            text(
                """
                INSERT INTO artifacts (id, org_id, ticket_id, stage_name, run_id,
                    stage_execution_id, version, iteration, is_final, body,
                    created_at)
                VALUES (:id, :org_id, :ticket_id, :stage_name, :run_id,
                    :stage_exec_id, 1, 0, true, 'old artifact body',
                    '2025-01-01 00:00:00+00')
                """
            ),
            {
                "id": prior_artifact_id,
                "org_id": org_id,
                "ticket_id": ticket_id,
                "stage_name": _STAGE_NAME,
                "run_id": prior_run_id,
                "stage_exec_id": prior_stage_exec_id,
            },
        )
        await db_session.flush()

        # Add attachment AFTER the prior artifact → newer → should adopt.
        att = await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements-v2.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=False),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_cmd = run.pending_agent_command_id
        assert provision_cmd is not None

        await _record(
            org_id, _success_event(provision_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs runs (attachment present).
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        assert seed_cmd is not None
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # With no review, adoption should immediately proceed to cleanup.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.phase == "cleanup"

        # Artifact for this new run must be the adopted one.
        new_artifact = (
            await db_session.execute(
                text(
                    "SELECT adopted_from_attachment_id, body "
                    "FROM artifacts WHERE run_id = :run_id AND stage_name = :stage_name"
                ),
                {"run_id": run_id, "stage_name": _STAGE_NAME},
            )
        ).one()
        assert new_artifact.adopted_from_attachment_id == att.id
        assert new_artifact.body == _FRONTMATTER_BODY


# ---------------------------------------------------------------------------
# Test (d): re-kickoff WITHOUT re-attach → artifact newer → main runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rekickoff_without_reattach_main_runs(db_session: AsyncSession) -> None:
    """After run-1 adopts an attachment (artifact row is now newer than the
    attachment), run-2 on the same ticket WITHOUT re-attaching should detect
    that the existing artifact is newer than the attachment → no adoption →
    main skill runs for real."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=False),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        # Run 1: adoption expected (att.attached_at > no prior artifact).
        run_id_1 = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="first kickoff",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run1 = await db_session.get(PipelineRunRow, run_id_1)
        assert run1 is not None
        prov1 = run1.pending_agent_command_id
        assert prov1 is not None

        await _record(
            org_id, _success_event(prov1, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs runs.
        run1 = await db_session.get(PipelineRunRow, run_id_1)
        assert run1 is not None
        seed1 = run1.pending_agent_command_id
        assert seed1 is not None
        await _record(
            org_id, _success_event(seed1, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # Run 1: adoption → cleanup.
        run1 = await db_session.get(PipelineRunRow, run_id_1)
        assert run1 is not None
        assert run1.phase == "cleanup"
        await _finish_via_cleanup(org_id, run_id_1, db_session)

        run1 = await db_session.get(PipelineRunRow, run_id_1)
        assert run1 is not None
        assert run1.state == "completed"

        # Run 2: same attachment (not re-attached). start_manual_run includes
        # current attachments in the snapshot — the same att.id is in the kickoff.
        run_id_2 = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="second kickoff",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run2 = await db_session.get(PipelineRunRow, run_id_2)
        assert run2 is not None
        prov2 = run2.pending_agent_command_id
        assert prov2 is not None

        await _record(
            org_id, _success_event(prov2, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs runs.
        run2 = await db_session.get(PipelineRunRow, run_id_2)
        assert run2 is not None
        seed2 = run2.pending_agent_command_id
        assert seed2 is not None
        await _record(
            org_id, _success_event(seed2, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # Run 2: artifact from run-1 is newer → main skill dispatches.
        run2 = await db_session.get(PipelineRunRow, run_id_2)
        assert run2 is not None
        main_cmd = run2.pending_agent_command_id
        assert main_cmd is not None

        stage_rows = (
            (
                await db_session.execute(
                    select(StageExecutionRow).where(
                        StageExecutionRow.run_id == run_id_2, StageExecutionRow.kind == "skill"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stage_rows) == 1
        assert stage_rows[0].phase == "main", "run-2 should run main skill, not adopt"


# ---------------------------------------------------------------------------
# Test (e): review return stamps confidence iff NULL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_return_stamps_confidence_when_null(db_session: AsyncSession) -> None:
    """After adoption, stage_exec.confidence is NULL; the review return should
    stamp it via bucket_confidence(review_return.confidence).
    """
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=True),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        prov_cmd = run.pending_agent_command_id
        assert prov_cmd is not None

        await _record(
            org_id, _success_event(prov_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs:
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        assert seed_cmd is not None
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # Now parked on review (after adoption).
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        review_cmd = run.pending_agent_command_id
        assert review_cmd is not None

        # Confirm confidence is NULL before review fires.
        stage_rows = (
            (
                await db_session.execute(
                    select(StageExecutionRow).where(
                        StageExecutionRow.run_id == run_id, StageExecutionRow.kind == "skill"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stage_rows) == 1
        stage_exec_id = stage_rows[0].id
        assert stage_rows[0].confidence is None

        # Fire review with confidence=55 → should bucket to "medium".
        # Pass as "stdout" so the stub's parse_result returns it as
        # result.output, which the run sink forwards as outputs["output"].
        review_outputs = {"stdout": _review_return_json(confidence=55)}
        await _record(
            org_id,
            _success_event(review_cmd, outputs=review_outputs),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        # Confidence should now be "medium" (55 ∈ [50, 90)).
        stage_exec_after = (
            await db_session.execute(select(StageExecutionRow).where(StageExecutionRow.id == stage_exec_id))
        ).scalar_one()
        assert stage_exec_after.confidence == "medium"


# ---------------------------------------------------------------------------
# Test (f): adopted stage with review off → boundary with NULL confidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopted_no_review_boundary_null_confidence(db_session: AsyncSession) -> None:
    """Adopted stage with no review and mode=conditional+on_confidence_below=medium:
    boundary settles immediately with NULL confidence — on_confidence_below must
    not trip (the condition can't fire for None confidence), and the run completes."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        # Use `on_confidence_below="medium"` with mode="conditional" — this
        # would trip on a normal low-confidence run, but NOT on adopted (NULL).
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=PipelineDefinition(
                name=f"pipe-{uuid4().hex[:8]}",
                stages=(
                    SkillStage(
                        name=_STAGE_NAME,
                        skill_name=_SKILL_NAME,
                        coding_agent_plugin_id="claude_code",
                        model="sonnet",
                        effort="medium",
                        review=None,
                        boundary=BoundaryControl(
                            mode="conditional",
                            on_confidence_below="medium",
                        ),
                    ),
                ),
            ),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        prov_cmd = run.pending_agent_command_id

        await _record(
            org_id, _success_event(prov_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs runs.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        assert seed_cmd is not None
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # Adoption with no review → boundary with NULL confidence → proceeds
        # (on_confidence_below="medium" does NOT trip for None).
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        # Should be in cleanup (proceeded, not paused).
        assert run.phase == "cleanup", f"expected cleanup phase (proceeded), got {run.phase}"

        # Complete cleanup.
        run = await _finish_via_cleanup(org_id, run_id, db_session)
        assert run.state == "completed"


# ---------------------------------------------------------------------------
# Test (g): fix loop — review blocker on adopted artifact dispatches main
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopted_review_blocker_dispatches_fix(db_session: AsyncSession) -> None:
    """After adoption, a review pass returns a blocker finding; the fix loop
    dispatches the main skill with the residuals as the revision (same
    fix-loop mechanics as a normal main-skill stage)."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=PipelineDefinition(
                name=f"pipe-{uuid4().hex[:8]}",
                stages=(
                    SkillStage(
                        name=_STAGE_NAME,
                        skill_name=_SKILL_NAME,
                        coding_agent_plugin_id="claude_code",
                        model="sonnet",
                        effort="medium",
                        review=ReviewConfig(
                            skill_name=_REVIEW_SKILL,
                            max_iterations=2,
                        ),
                        boundary=BoundaryControl(mode="always_proceed"),
                    ),
                ),
            ),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        prov_cmd = run.pending_agent_command_id

        await _record(
            org_id, _success_event(prov_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs:
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        assert seed_cmd is not None
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # After adoption, parked on review command.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        review_cmd_1 = run.pending_agent_command_id
        assert review_cmd_1 is not None

        # Fire review-1 with a blocker finding.
        review_with_blocker = json.dumps(
            {
                "confidence": 60,
                "new_findings": [
                    {
                        "severity": "blocker",
                        "body": "missing section",
                        "category": "spec",
                    }
                ],
                "prior_finding_verdicts": [],
                "summary": "found blocker",
            }
        )
        await _record(
            org_id,
            _success_event(review_cmd_1, outputs={"stdout": review_with_blocker}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        # Fix invocation should now be dispatched (the main skill re-runs).
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        fix_cmd = run.pending_agent_command_id
        assert fix_cmd is not None

        stage_rows = (
            (
                await db_session.execute(
                    select(StageExecutionRow).where(
                        StageExecutionRow.run_id == run_id, StageExecutionRow.kind == "skill"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stage_rows) == 1
        stage_exec = stage_rows[0]
        assert stage_exec.phase == "fix", "should be dispatching fix after blocker"
        assert stage_exec.iteration == 1

        # Residual finding exists.
        findings = await list_for_stage_execution(stage_exec.id, session=db_session)
        open_findings = [f for f in findings if f.status == "open"]
        assert len(open_findings) == 1
        assert open_findings[0].severity == "blocker"


# ---------------------------------------------------------------------------
# Tests (h) and (i): skill-version mismatch / match warning
# ---------------------------------------------------------------------------

_MISMATCHED_VERSION_BODY = (
    "---\n"
    "yaaos_artifact_version: 1\n"
    f"skill: {_SKILL_NAME}\n"
    "skill_version: '0.9.0'\n"  # older than shipped 1.0.0 → triggers warning
    "artifact_type: requirements\n"
    "produced_at: '2026-01-01T00:00:00Z'\n"
    "---\n\n"
    "# Requirements\n\n"
    "Feature: login page\n"
)


@pytest.mark.asyncio
async def test_skill_version_mismatch_logs_warning_and_adoption_proceeds(
    db_session: AsyncSession,
) -> None:
    """Attachment with an older skill_version triggers a warning but adoption
    still completes (run advances past the stage normally)."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_MISMATCHED_VERSION_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=False),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        # Provision.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_cmd = run.pending_agent_command_id
        await _record(
            org_id, _success_event(provision_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        # seed-inputs.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )

        # Capture logs during the drain that fires _try_adopt_for_stage.
        with capture_logs() as logs:
            await drain(db_session)

    mismatch_warnings = [e for e in logs if e.get("event") == "stage.adoption.skill_version_mismatch"]
    assert len(mismatch_warnings) == 1, "expected exactly one mismatch warning"
    w = mismatch_warnings[0]
    assert w["log_level"] == "warning"
    assert w["stage_name"] == _STAGE_NAME
    assert w["attachment_skill_version"] == "0.9.0"
    assert w["shipped_skill_version"] == "1.0.0"

    # Adoption must still have proceeded — run should be in cleanup (boundary
    # was always_proceed, no review).
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "cleanup", "adoption should have completed the stage"


@pytest.mark.asyncio
async def test_skill_version_match_no_warning(db_session: AsyncSession) -> None:
    """Attachment whose skill_version matches the shipped skill version produces
    no mismatch warning."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        # _FRONTMATTER_BODY has skill_version: '1.0.0' — matches shipped version.
        await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="requirements.md",
            body=_FRONTMATTER_BODY,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_adoption_stage_definition(with_review=False),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_cmd = run.pending_agent_command_id
        await _record(
            org_id, _success_event(provision_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        seed_cmd = run.pending_agent_command_id
        await _record(
            org_id, _success_event(seed_cmd, outputs={}), agent_id=agent_row["id"], db_session=db_session
        )

        with capture_logs() as logs:
            await drain(db_session)

    mismatch_warnings = [e for e in logs if e.get("event") == "stage.adoption.skill_version_mismatch"]
    assert len(mismatch_warnings) == 0, "no warning expected when versions match"
