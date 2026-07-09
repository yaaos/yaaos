"""Service test: real boundary evaluation, pause creation, and
approve/kill resolution.

Acceptance: an `always_hitl` stage sees run `paused` + an open `run_pauses`
row with tripped conditions + a notification to the escalation user;
`{action:"approve"}` from an escalation member continues to the next stage
(here: to completion, the only stage); `{action:"approve"}` from a
non-member raises `NotEscalationTargetError`; `{action:"kill"}` lands the
run `killed` with the ticket transitioned. Also covers: conditional trips
(`on_confidence_below`, `on_protected_code` — the latter folding the
protected set's owner into the pause's escalation set via
`repos.put_settings`), and immediate cancel of a `paused` run.

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
from app.core.notifications import list_for_user
from app.core.tenancy import create_membership, create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.pipelines import (
    BoundaryControl,
    Kickoff,
    NotEscalationTargetError,
    PauseResolution,
    PipelineDefinition,
    SkillStage,
    create_pipeline,
    request_cancel,
    resolve_pause,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow, RunPauseRow
from app.domain.pipelines.test.drain import drain
from app.domain.repos import ProtectedPathSet, RepoSettingsSpec, put_settings
from app.domain.tickets import create_from_pr
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_STAGE_NAME = "write-spec"
_REPO_EXTERNAL_ID = "acme/repo"


async def _seed_org_ticket_and_users(db_session) -> tuple[UUID, UUID, UUID, UUID]:
    """`(org_id, ticket_id, requester_id, stranger_id)`. `requester_id` is
    the kickoff actor (and thus the sole resolved escalation target absent
    a protected-code trip); `stranger_id` is a plain builder with no
    escalation standing."""
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    requester = await create_user(db_session, display_name="Requester")
    await create_membership(
        db_session, user_id=requester.id, org_id=org.org_id, role=Role.BUILDER, handle="requester"
    )
    stranger = await create_user(db_session, display_name="Stranger")
    await create_membership(
        db_session, user_id=stranger.id, org_id=org.org_id, role=Role.BUILDER, handle="stranger"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="boundary test ticket",
        description=None,
        repo_external_id=_REPO_EXTERNAL_ID,
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
    return org.org_id, ticket_id, requester.id, stranger.id


def _one_skill_stage_definition(boundary: BoundaryControl) -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_STAGE_NAME,
                skill_name=_STAGE_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=boundary,
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


async def _advance_to_paused(
    db_session,
    *,
    boundary: BoundaryControl,
    paths_affected: list[str] | None = None,
    confidence: int = 90,
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """Drive a one-skill-stage run from `start_run` through the main skill's
    terminal event, with the given `BoundaryControl`. Returns
    `(org_id, ticket_id, requester_id, stranger_id, run_id)` with the run
    asserted `paused`."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, requester_id, stranger_id = await _seed_org_ticket_and_users(db_session)
        agent_row = await seed_agent(org_id=org_id)

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_one_skill_stage_definition(boundary),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(
            intake_point_id="test", actor=Actor.user(user_id=requester_id), input_text="spec please"
        )
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

        skill_output = json.dumps(
            {
                "outcome": "completed",
                "confidence": confidence,
                "paths_affected": paths_affected or [],
                "summary": "wrote the spec",
            }
        )
        await _record(
            org_id,
            _success_event(
                skill_command_id, outputs={"stdout": skill_output, "exit_code": 0}, artifact_body="# Spec"
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "paused"
        return org_id, ticket_id, requester_id, stranger_id, run_id


async def _open_pause(db_session, run_id: UUID) -> RunPauseRow:
    return (
        await db_session.execute(
            select(RunPauseRow).where(RunPauseRow.run_id == run_id, RunPauseRow.resolved_at.is_(None))
        )
    ).scalar_one()


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
async def test_always_hitl_pauses_run_and_notifies_escalation_user_service(db_session) -> None:
    org_id, ticket_id, requester_id, _stranger_id, run_id = await _advance_to_paused(
        db_session, boundary=BoundaryControl(mode="always_hitl")
    )

    pause = await _open_pause(db_session, run_id)
    assert pause.tripped == {"always_hitl": True}
    assert set(pause.escalation_user_ids) == {requester_id}

    ticket_status = (
        await db_session.execute(text("SELECT status FROM tickets WHERE id = :id"), {"id": ticket_id})
    ).scalar_one()
    assert ticket_status == "hitl"

    notifications = await list_for_user(db_session, user_id=requester_id, org_id=org_id)
    assert any(n.type == "pipeline_run_paused" and n.subject_id == pause.id for n in notifications)


@pytest.mark.asyncio
async def test_approve_by_escalation_actor_resumes_to_completion_service(db_session) -> None:
    org_id, ticket_id, requester_id, _stranger_id, run_id = await _advance_to_paused(
        db_session, boundary=BoundaryControl(mode="always_hitl")
    )
    pause = await _open_pause(db_session, run_id)

    async with org_context(org_id, ActorKind.SYSTEM):
        await resolve_pause(
            pause.id,
            resolution=PauseResolution(action="approve"),
            actor=Actor.user(user_id=requester_id),
            session=db_session,
        )
    await db_session.commit()
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "completed"

    ticket_status = (
        await db_session.execute(text("SELECT status FROM tickets WHERE id = :id"), {"id": ticket_id})
    ).scalar_one()
    assert ticket_status == "done"

    pause_row = await db_session.get(RunPauseRow, pause.id)
    assert pause_row is not None
    assert pause_row.resolution == "approve"
    assert pause_row.resolved_at is not None


@pytest.mark.asyncio
async def test_non_responder_gets_not_escalation_target_error_service(db_session) -> None:
    org_id, _ticket_id, _requester_id, stranger_id, run_id = await _advance_to_paused(
        db_session, boundary=BoundaryControl(mode="always_hitl")
    )
    pause = await _open_pause(db_session, run_id)

    with pytest.raises(NotEscalationTargetError):
        async with org_context(org_id, ActorKind.SYSTEM):
            await resolve_pause(
                pause.id,
                resolution=PauseResolution(action="approve"),
                actor=Actor.user(user_id=stranger_id),
                session=db_session,
            )


@pytest.mark.asyncio
async def test_kill_resolution_terminates_run_service(db_session) -> None:
    org_id, ticket_id, requester_id, _stranger_id, run_id = await _advance_to_paused(
        db_session, boundary=BoundaryControl(mode="always_hitl")
    )
    pause = await _open_pause(db_session, run_id)

    async with org_context(org_id, ActorKind.SYSTEM):
        await resolve_pause(
            pause.id,
            resolution=PauseResolution(action="kill"),
            actor=Actor.user(user_id=requester_id),
            session=db_session,
        )
    await db_session.commit()
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "killed"

    ticket_status = (
        await db_session.execute(text("SELECT status FROM tickets WHERE id = :id"), {"id": ticket_id})
    ).scalar_one()
    assert ticket_status == "cancelled"

    pause_row = await db_session.get(RunPauseRow, pause.id)
    assert pause_row is not None
    assert pause_row.resolution == "kill"
    assert pause_row.resolved_at is not None


@pytest.mark.asyncio
async def test_cancel_while_paused_is_immediate_service(db_session) -> None:
    org_id, _ticket_id, _requester_id, _stranger_id, run_id = await _advance_to_paused(
        db_session, boundary=BoundaryControl(mode="always_hitl")
    )
    pause = await _open_pause(db_session, run_id)

    async with org_context(org_id, ActorKind.SYSTEM):
        await request_cancel(run_id, actor=Actor.system(), session=db_session)
    await db_session.commit()
    await drain(db_session)

    run = await _finish_via_cleanup(org_id, run_id, db_session)
    assert run.state == "cancelled"

    pause_row = await db_session.get(RunPauseRow, pause.id)
    assert pause_row is not None
    assert pause_row.resolved_at is not None
    assert pause_row.resolution is None


@pytest.mark.asyncio
async def test_confidence_below_condition_trips_pause_service(db_session) -> None:
    _org_id, _ticket_id, _requester_id, _stranger_id, run_id = await _advance_to_paused(
        db_session,
        boundary=BoundaryControl(mode="conditional", on_confidence_below="medium"),
        confidence=10,
    )
    pause = await _open_pause(db_session, run_id)
    assert pause.tripped == {"confidence_below": "medium"}


@pytest.mark.asyncio
async def test_protected_code_trip_adds_owner_to_escalation_service(db_session) -> None:
    """A `repos.put_settings`-configured protected path set, matched by the
    stage's reported `paths_affected`, trips `on_protected_code` and folds
    the set's owner into the pause's escalation set alongside the kickoff
    actor."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id, requester_id, _stranger_id = await _seed_org_ticket_and_users(db_session)
        owner = await create_user(db_session, display_name="Path Owner")
        await create_membership(
            db_session, user_id=owner.id, org_id=org_id, role=Role.BUILDER, handle="owner"
        )
        await put_settings(
            org_id,
            _REPO_EXTERNAL_ID,
            settings=RepoSettingsSpec(
                protected_mode="deny",
                protected_path_sets=(
                    ProtectedPathSet(id=uuid4(), globs=("infra/**",), owner_user_ids=(owner.id,)),
                ),
            ),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        agent_row = await seed_agent(org_id=org_id)
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_one_skill_stage_definition(
                BoundaryControl(mode="conditional", on_protected_code=True)
            ),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(
            intake_point_id="test", actor=Actor.user(user_id=requester_id), input_text="touch infra"
        )
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
        skill_output = json.dumps(
            {
                "outcome": "completed",
                "confidence": 95,
                "paths_affected": ["infra/prod.tf"],
                "summary": "touched infra",
            }
        )
        await _record(
            org_id,
            _success_event(
                skill_command_id, outputs={"stdout": skill_output, "exit_code": 0}, artifact_body="# Infra"
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "paused"

        pause = await _open_pause(db_session, run_id)
        assert pause.tripped == {"protected_code": True}
        assert set(pause.escalation_user_ids) == {requester_id, owner.id}
