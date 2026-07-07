"""Service test: the shipped `troubleshoot` default pipeline
(diagnose -> fix-plan -> call(implementation)) end-to-end, materialized
from the template via `instantiate_template` — proving the composition
(stage name `fix-plan` runs the `plan` skill, distinct identity from
`dev`'s own `plan` stage), not a hand-built stand-in definition.

Acceptance: a bug-report kickoff produces diagnose/fix-plan artifacts in
order, the composed `implementation` tail runs `implement` (with its
`code-review` review pass) and opens a pull request via
`github:create_pr` on a live `apps/fake-github` subprocess.

A second test proves `troubleshoot`'s OWN stage names support the same
conditional-boundary pause/resolve mechanic `dev`'s test exercises —
`register_stub_vcs` here since it never reaches the PR action.
(`send_back`, `instruct`-on-pause, `start_rerun_from_stage`, and
`request_cancel` are exercised against the `dev` pipeline in
`test_dev_pipeline_service.py` — the mechanics are pipeline-agnostic engine
behavior, already proven once against a shipped definition is sufficient;
this file's second test rounds out coverage of `troubleshoot`'s distinct
stage names.)

Uses the shared `drain` outbox-dispatch helper (`test/drain.py`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.agent_gateway import AgentEvent, AgentEventKind, record_agent_event
from app.core.agent_gateway import Artifact as WireArtifact
from app.core.audit_log import Actor, ActorKind
from app.core.auth import Role, org_context
from app.core.config import get_settings
from app.core.identity import create_user
from app.core.tenancy import create_membership, create_org
from app.core.vcs import list_yaaos_comments
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.artifacts import latest_final
from app.domain.findings import list_open_for_ticket
from app.domain.orgs import create_org as create_org_with_audit
from app.domain.pipelines import (
    Kickoff,
    PauseResolution,
    defaults,
    instantiate_template,
    resolve_pause,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr, get_pull_request
from app.domain.tickets import get as get_ticket
from app.plugins.github import GitHubPlugin, record_app_install
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REPO_EXTERNAL_ID = "acme/web"


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


def _skill_output(*, confidence: int, paths_affected: list[str] | None = None) -> str:
    return json.dumps(
        {
            "outcome": "completed",
            "confidence": confidence,
            "paths_affected": paths_affected or [],
            "summary": "done",
        }
    )


async def _stage_rows(db_session, run_id: UUID) -> list[StageExecutionRow]:
    """Ordered by `id` (uuid7 — monotonic on creation time), not
    `started_at`: every insert in this test runs inside one outer test
    transaction, so Postgres's `now()` (transaction start time) is
    IDENTICAL across every row and `started_at` alone is not a reliable
    creation-order tiebreaker."""
    return (
        (
            await db_session.execute(
                select(StageExecutionRow)
                .where(StageExecutionRow.run_id == run_id)
                .order_by(StageExecutionRow.id)
            )
        )
        .scalars()
        .all()
    )


async def _seed_ticket_and_user(
    db_session, org_id: UUID, *, ticket_title: str = "Fix the flaky checkout webhook"
) -> tuple[UUID, UUID]:
    user = await create_user(db_session, display_name="Requester")
    await create_membership(db_session, user_id=user.id, org_id=org_id, role=Role.BUILDER, handle="requester")
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"troubleshoot-ticket-{uuid4().hex[:8]}",
        title=ticket_title,
        description=None,
        repo_external_id=_REPO_EXTERNAL_ID,
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        branch_name=f"yaaos/{uuid4().hex[:8]}",
        session=db_session,
    )
    return ticket_id, user.id


async def _start_troubleshoot_run(
    db_session, org_id: UUID, *, ticket_id: UUID, requester_id: UUID
) -> tuple[UUID, UUID]:
    """Materialize `troubleshoot` from the template and start a run on it.
    Returns `(troubleshoot_pipeline_id, run_id)` with the run parked
    awaiting the PROVISION system stage's terminal event."""
    troubleshoot_pipeline_id = await instantiate_template(
        org_id=org_id, template_id=defaults.TROUBLESHOOT_ID, actor=Actor.system(), session=db_session
    )
    await db_session.flush()

    kickoff = Kickoff(
        intake_point_id="test",
        actor=Actor.user(user_id=requester_id),
        input_text="Webhook deliveries intermittently 500 under load; suspect a race on the idempotency check.",
    )
    run_id = await start_run(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=troubleshoot_pipeline_id,
        kickoff=kickoff,
        session=db_session,
    )
    await db_session.commit()
    return troubleshoot_pipeline_id, run_id


async def _complete_provision(org_id: UUID, run_id: UUID, agent_id: UUID, db_session) -> None:
    await drain(db_session)
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    provision_command_id = run.pending_agent_command_id
    assert provision_command_id is not None
    await _record(
        org_id, _success_event(provision_command_id, outputs={}), agent_id=agent_id, db_session=db_session
    )
    await drain(db_session)


async def _complete_skill_stage(
    org_id: UUID,
    run_id: UUID,
    db_session,
    *,
    artifact_body: str,
    confidence: int = 95,
    paths_affected: list[str] | None = None,
) -> None:
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    command_id = run.pending_agent_command_id
    assert command_id is not None
    await _record(
        org_id,
        _success_event(
            command_id,
            outputs={
                "stdout": _skill_output(confidence=confidence, paths_affected=paths_affected),
                "exit_code": 0,
            },
            artifact_body=artifact_body,
        ),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)


@pytest.fixture
async def github_org(db_session, monkeypatch: pytest.MonkeyPatch, fake_github_base_url: str) -> UUID:
    """An org with an active GitHub App installation pointed at a live
    `apps/fake-github` subprocess — same shape as
    `test_pr_actions_service.py`'s own `github_org` fixture."""
    org = await create_org_with_audit(
        db_session, slug=f"troubleshoot-org-{uuid4().hex[:8]}", display_name="Troubleshoot Org"
    )
    await record_app_install(
        db_session, org_id=org.id, install_external_id="install-fake-1", account_login="acme"
    )
    await db_session.flush()

    monkeypatch.setenv("YAAOS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("YAAOS_GITHUB_APP_PRIVATE_KEY", "fake-pem-not-a-real-key")
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos-test")
    get_settings.cache_clear()
    monkeypatch.setattr(GitHubPlugin, "base_url", fake_github_base_url)

    return org.id


@pytest.mark.asyncio
async def test_troubleshoot_pipeline_creates_pr_service(github_org: UUID, db_session) -> None:
    org_id = github_org
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
    agent_row = await seed_agent(org_id=org_id)

    _troubleshoot_id, run_id = await _start_troubleshoot_run(
        db_session, org_id, ticket_id=ticket_id, requester_id=requester_id
    )
    await _complete_provision(org_id, run_id, agent_row["id"], db_session)

    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Diagnosis\n\nRace on the idempotency check under load."
    )
    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Fix plan\n\n1. Serialize on the idempotency key."
    )

    # `implement` main dispatch — the call stage flattened straight in;
    # no review residuals this run (empty `new_findings`/`prior_finding_verdicts`)
    # so the loop settles on the first review pass — proceeds straight to
    # `github:create_pr`.
    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Implementation notes", paths_affected=["app.py"]
    )
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    review_command_id = run.pending_agent_command_id
    assert review_command_id is not None
    clean_review = json.dumps(
        {"new_findings": [], "prior_finding_verdicts": [], "confidence": 95, "summary": "looks solid"}
    )
    await _record(
        org_id,
        _success_event(review_command_id, outputs={"stdout": clean_review, "exit_code": 0}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "cleanup", run.failure_reason
    cleanup_command_id = run.pending_agent_command_id
    assert cleanup_command_id is not None
    await _record(
        org_id, _success_event(cleanup_command_id, outputs={}), agent_id=None, db_session=db_session
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "completed", run.failure_reason

    async with org_context(org_id, ActorKind.SYSTEM):
        diagnose_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="diagnose", session=db_session
        )
        fix_plan_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="fix-plan", session=db_session
        )
        implement_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="implement", session=db_session
        )
    assert diagnose_artifact is not None and "Race on the idempotency" in diagnose_artifact.body
    assert fix_plan_artifact is not None and "Serialize on the idempotency key" in fix_plan_artifact.body
    assert implement_artifact is not None

    stages = await _stage_rows(db_session, run_id)
    stage_names_in_order = [s.stage_name for s in stages if s.kind != "system"]
    assert stage_names_in_order == ["diagnose", "fix-plan", "implement", "github:create_pr"]

    findings = await list_open_for_ticket(org_id, ticket_id, session=db_session)
    assert findings == []

    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.pr_id is not None
    pr = await get_pull_request(ticket.pr_id, org_id=org_id)
    # `github:create_pr` opened the PR itself — its own idempotency and the
    # posting-reconciliation-after-crash paths are covered at
    # `apps/backend/app/plugins/github/test/test_pr_actions_service.py`.
    posted_comments = await list_yaaos_comments("github", org_id, pr.external_id)
    assert posted_comments == []  # no residual findings this run


@pytest.mark.asyncio
async def test_troubleshoot_pipeline_pause_at_diagnose_then_approve_service(db_session) -> None:
    """A low-confidence `diagnose` return trips the shipped `troubleshoot`
    definition's `on_confidence_below` boundary; `approve` continues to
    `fix-plan` without re-running `diagnose`."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    with register_stub_vcs(plugin_id="github"):
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
        org_id = org.org_id
        ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
        agent_row = await seed_agent(org_id=org_id)

        _troubleshoot_id, run_id = await _start_troubleshoot_run(
            db_session, org_id, ticket_id=ticket_id, requester_id=requester_id
        )
        await _complete_provision(org_id, run_id, agent_row["id"], db_session)

        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Diagnosis v1", confidence=25)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "paused"
        pause = (
            await db_session.execute(
                select(RunPauseRow).where(RunPauseRow.run_id == run_id, RunPauseRow.resolved_at.is_(None))
            )
        ).scalar_one()
        assert pause.tripped == {"confidence_below": "medium"}

        async with org_context(org_id, ActorKind.SYSTEM):
            await resolve_pause(
                pause.id,
                resolution=PauseResolution(action="approve"),
                actor=Actor.user(user_id=requester_id),
                session=db_session,
            )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "running"
        stages = await _stage_rows(db_session, run_id)
        # `diagnose` was never re-run — approve just moved forward to `fix-plan`.
        assert [s.stage_name for s in stages if s.kind != "system"] == ["diagnose", "fix-plan"]
