"""Service test: a one-skill-stage run drives real coding-agent dispatch.

Acceptance flow: `start_run` → `provision-workspace` system stage dispatches
and parks; a synthetic `completed_success` event (via `record_agent_event`)
resumes it and dispatches the skill stage; a synthetic `completed_success`
event carrying a `SkillReturn` JSON body + artifact resumes the skill stage,
storing + finalizing the artifact; `cleanup-workspace` dispatches and parks;
its synthetic `completed_success` event finalizes the run `completed`.

Plus: a `completed` outcome without an artifact body fails the stage; a
synthetic `completed_failure` event fails the run with the reported reason.

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

_STAGE_NAME = "write-spec"


async def _seed_org_ticket_and_user(db_session) -> tuple[UUID, UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    user = await create_user(db_session, display_name="Watcher")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="watcher"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="skill stage test ticket",
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


def _one_skill_stage_definition() -> PipelineDefinition:
    # `mode="always_proceed"` — this file exercises skill-stage dispatch
    # mechanics, not boundary policy; `BoundaryControl()`'s own default
    # (`always_hitl`) would pause every run here instead of completing it.
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_STAGE_NAME,
                skill_name=_STAGE_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
        ),
    )


def _two_skill_stage_definition() -> PipelineDefinition:
    """Two-stage pipeline used for liveness-check tests that need a second
    stage dispatch after the first stage completes."""
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_STAGE_NAME,
                skill_name=_STAGE_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
            SkillStage(
                name="review-spec",
                skill_name="review-spec",
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


def _failure_event(command_id: UUID, *, reason: str) -> AgentEvent:
    return AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_FAILURE,
        outcome_label=reason,
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )


async def _record(org_id: UUID, event: AgentEvent, *, agent_id: UUID | None, db_session) -> None:
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()


async def _advance_to_skill_dispatch(
    db_session,
    *,
    definition: PipelineDefinition | None = None,
) -> tuple[UUID, UUID, UUID, UUID]:
    """Drive `start_run` through the provision system stage's terminal event.

    Returns `(org_id, ticket_id, run_id, skill_command_id)` with the run
    parked awaiting the stage-0 skill stage's terminal event.

    `definition` defaults to `_one_skill_stage_definition()`; pass a multi-stage
    definition for tests that need subsequent stage dispatches.
    """
    if definition is None:
        definition = _one_skill_stage_definition()
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
            intake_point_id="test", actor=Actor.user(user_id=user_id), input_text="write the spec, please"
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
        assert run.workspace_id is not None
        skill_command_id = run.pending_agent_command_id
        assert skill_command_id is not None

        return org_id, ticket_id, run_id, skill_command_id


async def _finish_via_cleanup(org_id: UUID, run_id: UUID, db_session) -> PipelineRunRow:
    """The run provisioned a workspace, so every terminal — including a
    failure — routes through the `cleanup-workspace` system stage first.
    Feed its success event and drain to the real terminal state."""
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
async def test_skill_stage_acceptance_flow(db_session) -> None:
    """Provision → skill stage (stored+final artifact, version 1) → cleanup
    → run completed."""
    org_id, ticket_id, run_id, skill_command_id = await _advance_to_skill_dispatch(db_session)

    skill_output = json.dumps(
        {"outcome": "completed", "confidence": 87, "paths_affected": [], "summary": "wrote the spec"}
    )
    await _record(
        org_id,
        _success_event(
            skill_command_id, outputs={"stdout": skill_output, "exit_code": 0}, artifact_body="# Spec\n\nbody"
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "completed"

    stages = await _stage_rows(db_session, run_id)
    by_name = {s.stage_name: s for s in stages}
    assert by_name["provision-workspace"].kind == "system"
    assert by_name["provision-workspace"].status == "completed"
    assert by_name[_STAGE_NAME].kind == "skill"
    assert by_name[_STAGE_NAME].status == "completed"
    assert by_name["cleanup-workspace"].kind == "system"
    assert by_name["cleanup-workspace"].status == "completed"

    artifact_row = (
        await db_session.execute(
            text("SELECT version, is_final, body FROM artifacts WHERE ticket_id = :ticket_id"),
            {"ticket_id": ticket_id},
        )
    ).one()
    assert artifact_row.version == 1
    assert artifact_row.is_final is True
    assert artifact_row.body == "# Spec\n\nbody"


@pytest.mark.asyncio
async def test_completed_outcome_without_artifact_fails_stage(db_session) -> None:
    org_id, _, run_id, skill_command_id = await _advance_to_skill_dispatch(db_session)

    skill_output = json.dumps(
        {"outcome": "completed", "confidence": 90, "paths_affected": [], "summary": "wrote the spec"}
    )
    await _record(
        org_id,
        _success_event(skill_command_id, outputs={"stdout": skill_output, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "failed"

    stages = await _stage_rows(db_session, run_id)
    skill_row = next(s for s in stages if s.stage_name == _STAGE_NAME)
    assert skill_row.status == "failed"
    assert skill_row.failure_reason is not None
    assert "artifact" in skill_row.failure_reason


@pytest.mark.asyncio
async def test_infra_failure_event_fails_run_with_reason(db_session) -> None:
    org_id, _, run_id, skill_command_id = await _advance_to_skill_dispatch(db_session)

    await _record(
        org_id,
        _failure_event(skill_command_id, reason="claude exit 1: boom"),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "failed"
    assert run.failure_reason == "claude exit 1: boom"


@pytest.mark.asyncio
async def test_workspace_dead_before_stage_triggers_reprovision(db_session) -> None:
    """When the workspace dies between two stage dispatches the engine
    re-provisions before attempting the second stage."""
    with register_stub_vcs(plugin_id="github"):
        org_id, _ticket_id, run_id, first_skill_command_id = await _advance_to_skill_dispatch(
            db_session, definition=_two_skill_stage_definition()
        )

        # Capture workspace_id before completing stage 0.
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        workspace_id = run.workspace_id

        # Stage 0 succeeds.
        skill_output = json.dumps(
            {"outcome": "completed", "confidence": 80, "paths_affected": [], "summary": "done"}
        )
        await _record(
            org_id,
            _success_event(
                first_skill_command_id,
                outputs={"stdout": skill_output, "exit_code": 0},
                artifact_body="# first spec",
            ),
            agent_id=None,
            db_session=db_session,
        )

        # Expire the workspace before drain reaches START_STAGE for stage 1.
        await db_session.execute(
            text("UPDATE workspaces SET status = 'expired' WHERE id = :ws_id"),
            {"ws_id": workspace_id},
        )
        await db_session.commit()

        # Drain: HANDLE_AGENT_EVENT → ROUTE_RUN → START_STAGE (sees expired
        # workspace → re-provisions instead of dispatching stage 1 directly).
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.phase == "provision"
        # New workspace_id was minted for the re-provision.
        assert run.workspace_id != workspace_id

        stages = await _stage_rows(db_session, run_id)
        provision_rows = [s for s in stages if s.stage_name == "provision-workspace"]
        assert len(provision_rows) == 2  # initial provision + re-provision for stage 1


@pytest.mark.asyncio
async def test_auth_expired_dispatches_real_refresh_command(db_session) -> None:
    """The auth-refresh recovery dispatch must enqueue a real
    `RefreshWorkspaceAuth` AgentCommand carrying a freshly-minted token —
    not a `CleanupWorkspace` command, which would tear the workspace down
    instead of rotating credentials."""
    org_id, _, run_id, skill_command_id = await _advance_to_skill_dispatch(db_session)

    # `dispatch_auth_refresh` mints a fresh installation token via `core/vcs`,
    # so the VCS stub must be active for this drain (unlike a plain
    # CleanupWorkspace dispatch, which never touches VCS).
    with register_stub_vcs(plugin_id="github"):
        await _record(
            org_id,
            _failure_event(skill_command_id, reason="auth_expired: token expired"),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    refresh_command_id = run.pending_agent_command_id
    assert refresh_command_id is not None

    row = (
        await db_session.execute(
            text("SELECT command_kind, payload FROM agent_commands WHERE id = :id"),
            {"id": refresh_command_id},
        )
    ).one()
    assert row.command_kind == "RefreshWorkspaceAuth"
    assert row.payload.get("new_token")


@pytest.mark.asyncio
async def test_auth_expired_triggers_refresh_and_retry(db_session) -> None:
    """An auth_expired failure inserts a refresh-auth system row, dispatches
    dispatch_auth_refresh, and on success retries the skill invocation.
    A successful retry drives the run to completion."""
    org_id, _, run_id, skill_command_id = await _advance_to_skill_dispatch(db_session)

    # Feed auth_expired failure on the skill stage. `dispatch_auth_refresh`
    # mints a fresh installation token via `core/vcs`, so the stub must be
    # active for this drain.
    with register_stub_vcs(plugin_id="github"):
        await _record(
            org_id,
            _failure_event(skill_command_id, reason="auth_expired: token expired"),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

    # Run is now parked on the refresh-auth command.
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "running"
    assert run.phase == "stages"
    refresh_command_id = run.pending_agent_command_id
    assert refresh_command_id is not None

    stages = await _stage_rows(db_session, run_id)
    refresh_rows = [s for s in stages if s.stage_name == "refresh-auth"]
    assert len(refresh_rows) == 1
    assert refresh_rows[0].kind == "system"
    assert refresh_rows[0].status == "running"
    skill_rows = [s for s in stages if s.stage_name == _STAGE_NAME]
    assert len(skill_rows) == 1
    assert skill_rows[0].status == "failed"
    assert "auth_expired" in (skill_rows[0].failure_reason or "")

    # Refresh-auth succeeds → engine retries the skill stage.
    await _record(
        org_id,
        _success_event(refresh_command_id, outputs={}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "running"
    retry_skill_command_id = run.pending_agent_command_id
    assert retry_skill_command_id is not None
    assert retry_skill_command_id != skill_command_id  # new command minted for retry

    # Retry succeeds with an artifact.
    retry_output = json.dumps(
        {"outcome": "completed", "confidence": 90, "paths_affected": [], "summary": "done after refresh"}
    )
    await _record(
        org_id,
        _success_event(
            retry_skill_command_id,
            outputs={"stdout": retry_output, "exit_code": 0},
            artifact_body="# Spec after refresh",
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "completed"

    stages = await _stage_rows(db_session, run_id)
    all_skill_rows = [s for s in stages if s.stage_name == _STAGE_NAME]
    assert len(all_skill_rows) == 2  # original failed attempt + successful retry
    assert any(s.status == "completed" for s in all_skill_rows)
    assert next(s for s in stages if s.stage_name == "refresh-auth").status == "completed"


@pytest.mark.asyncio
async def test_auth_expired_retry_cap_fails_on_second_attempt(db_session) -> None:
    """A second auth_expired after the one-retry has already been consumed
    hits the cap: the stage and run fail normally without another refresh."""
    org_id, _, run_id, skill_command_id = await _advance_to_skill_dispatch(db_session)

    # First auth_expired → refresh-auth dispatched. `dispatch_auth_refresh`
    # mints a fresh installation token via `core/vcs`, so the stub must be
    # active for this drain.
    with register_stub_vcs(plugin_id="github"):
        await _record(
            org_id,
            _failure_event(skill_command_id, reason="auth_expired: round 1"),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    refresh_command_id = run.pending_agent_command_id

    # Refresh succeeds → retry skill dispatched.
    await _record(
        org_id,
        _success_event(refresh_command_id, outputs={}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    retry_command_id = run.pending_agent_command_id
    assert retry_command_id is not None

    # Second auth_expired on the retry — cap hit, no more retries.
    await _record(
        org_id,
        _failure_event(retry_command_id, reason="auth_expired: round 2"),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "failed"
    assert run.failure_reason is not None
    assert "auth_expired" in run.failure_reason

    stages = await _stage_rows(db_session, run_id)
    all_skill_rows = [s for s in stages if s.stage_name == _STAGE_NAME]
    assert len(all_skill_rows) == 2  # original failed attempt + capped retry attempt
    assert all(s.status == "failed" for s in all_skill_rows)
