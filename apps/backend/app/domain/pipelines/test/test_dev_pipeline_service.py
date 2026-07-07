"""Service test: the shipped `dev` default pipeline
(requirements -> architecture -> plan -> call(implementation)) end-to-end,
materialized from the template via `instantiate_template` — proving the
composition, not a hand-built stand-in definition.

Acceptance: a spec kickoff produces requirements/architecture/plan artifacts
in order, `implement` runs with its `code-review` review pass, and
`github:create_pr` opens a pull request on a live `apps/fake-github`
subprocess carrying the residual finding
(`test_dev_pipeline_creates_pr_with_residual_finding_service`).

The remaining tests exercise run mechanics against these SAME shipped stage
names (not generic test stages): a conditional boundary pause resolved by
`approve`, a second one resolved by `instruct`, a main-skill `send_back` to
an upstream planning stage, `start_rerun_from_stage` on a completed run, and
`request_cancel` on a running run. These use `register_stub_vcs` (no need
to reach the PR action) rather than the live fake-github subprocess.

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
    StageNotInDefinitionError,
    defaults,
    instantiate_template,
    request_cancel,
    resolve_pause,
    start_rerun_from_stage,
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
    db_session, org_id: UUID, *, ticket_title: str = "Add rate limiting"
) -> tuple[UUID, UUID]:
    """`(ticket_id, requester_id)` — a yaaos-authored ticket (no upstream
    PR), branch pre-minted the way a dev-pipeline intake would."""
    user = await create_user(db_session, display_name="Requester")
    await create_membership(db_session, user_id=user.id, org_id=org_id, role=Role.BUILDER, handle="requester")
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"dev-ticket-{uuid4().hex[:8]}",
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


async def _start_dev_run(
    db_session, org_id: UUID, *, ticket_id: UUID, requester_id: UUID
) -> tuple[UUID, UUID]:
    """Materialize `dev` from the template and start a run on it. Returns
    `(dev_pipeline_id, run_id)` with the run parked awaiting the PROVISION
    system stage's terminal event."""
    dev_pipeline_id = await instantiate_template(
        org_id=org_id, template_id=defaults.DEV_ID, actor=Actor.system(), session=db_session
    )
    await db_session.flush()

    kickoff = Kickoff(
        intake_point_id="test",
        actor=Actor.user(user_id=requester_id),
        input_text="Add a token-bucket rate limiter to the public API.",
    )
    run_id = await start_run(
        org_id=org_id, ticket_id=ticket_id, pipeline_id=dev_pipeline_id, kickoff=kickoff, session=db_session
    )
    await db_session.commit()
    return dev_pipeline_id, run_id


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
    """Complete whatever skill stage the run is currently parked on with a
    plain `outcome=completed` return, then drain."""
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
        db_session, slug=f"dev-pipeline-org-{uuid4().hex[:8]}", display_name="Dev Pipeline Org"
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
async def test_dev_pipeline_creates_pr_with_residual_finding_service(github_org: UUID, db_session) -> None:
    org_id = github_org
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
    agent_row = await seed_agent(org_id=org_id)

    _dev_pipeline_id, run_id = await _start_dev_run(
        db_session, org_id, ticket_id=ticket_id, requester_id=requester_id
    )
    await _complete_provision(org_id, run_id, agent_row["id"], db_session)

    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Requirements\n\nRate limit the API."
    )
    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Architecture\n\nToken bucket per-key."
    )
    await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Plan\n\n1. Add middleware.")

    # `implement` main dispatch — the call stage flattened straight in.
    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Implementation notes", paths_affected=["app.py"]
    )

    # `implementation`'s review loop caps at `max_iterations=3`. The residual
    # is reported once (iteration 1) and never fixed or verdicted, so it
    # stays open across the remaining passes (residuals are queried by
    # `source_stage_execution_id`, not re-derived from the latest review
    # output) — iterations 2 and 3 report nothing new, the fix dispatch in
    # between just re-submits the same artifact, and the cap forces
    # settlement with the residual still open: exactly what reaches
    # `github:create_pr`.
    first_review_output = json.dumps(
        {
            "new_findings": [
                {
                    "severity": "should_fix",
                    "body": "the limiter has no test covering the burst case",
                    "code_file": "app.py",
                    "code_line": 42,
                }
            ],
            "prior_finding_verdicts": [],
            "confidence": 92,
            "summary": "one non-blocking gap",
        }
    )
    no_change_review_output = json.dumps(
        {"new_findings": [], "prior_finding_verdicts": [], "confidence": 92, "summary": "gap remains"}
    )
    for iteration in range(1, 4):
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        review_command_id = run.pending_agent_command_id
        assert review_command_id is not None
        await _record(
            org_id,
            _success_event(
                review_command_id,
                outputs={
                    "stdout": first_review_output if iteration == 1 else no_change_review_output,
                    "exit_code": 0,
                },
            ),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)
        if iteration < 3:
            run = await db_session.get(PipelineRunRow, run_id)
            assert run is not None
            fix_command_id = run.pending_agent_command_id
            assert fix_command_id is not None
            await _record(
                org_id,
                _success_event(
                    fix_command_id,
                    outputs={
                        "stdout": _skill_output(confidence=92, paths_affected=["app.py"]),
                        "exit_code": 0,
                    },
                    artifact_body=f"# Implementation notes v{iteration + 1}",
                ),
                agent_id=None,
                db_session=db_session,
            )
            await drain(db_session)

    # High confidence + a should_fix (not blocker) residual + an
    # unconfigured protected-path set never trips `implementation`'s
    # conditional boundary — proceeds straight to `github:create_pr`, which
    # runs synchronously in this same drain cycle, then dispatches cleanup.
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
        requirements_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="requirements", session=db_session
        )
        architecture_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="architecture", session=db_session
        )
        plan_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="plan", session=db_session
        )
        implement_artifact = await latest_final(
            org_id=org_id, ticket_id=ticket_id, stage_name="implement", session=db_session
        )
    assert requirements_artifact is not None and "Rate limit" in requirements_artifact.body
    assert architecture_artifact is not None and "Token bucket" in architecture_artifact.body
    assert plan_artifact is not None and "middleware" in plan_artifact.body
    assert implement_artifact is not None

    stages = await _stage_rows(db_session, run_id)
    stage_names_in_order = [s.stage_name for s in stages if s.kind != "system"]
    assert stage_names_in_order == ["requirements", "architecture", "plan", "implement", "github:create_pr"]

    findings = await list_open_for_ticket(org_id, ticket_id, session=db_session)
    assert len(findings) == 1
    assert findings[0].severity == "should_fix"
    assert findings[0].external_comment_id is not None

    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.pr_id is not None
    pr = await get_pull_request(ticket.pr_id, org_id=org_id)

    posted_comments = await list_yaaos_comments("github", org_id, pr.external_id)
    assert any(findings[0].handle in c.body for c in posted_comments)


@pytest.mark.asyncio
async def test_dev_pipeline_pause_approve_then_pause_instruct_service(db_session) -> None:
    """A low-confidence `requirements` return trips `on_confidence_below`;
    `approve` continues to `architecture` without re-running `requirements`.
    A low-confidence `architecture` return pauses again; `instruct` creates
    a NEW `stage_executions` row at architecture's SAME index carrying the
    human's revision, continuing the same run."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    with register_stub_vcs(plugin_id="github"):
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
        org_id = org.org_id
        ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
        agent_row = await seed_agent(org_id=org_id)

        _dev_id, run_id = await _start_dev_run(
            db_session, org_id, ticket_id=ticket_id, requester_id=requester_id
        )
        await _complete_provision(org_id, run_id, agent_row["id"], db_session)

        await _complete_skill_stage(
            org_id, run_id, db_session, artifact_body="# Requirements v1", confidence=20
        )

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
        # `requirements` was never re-run — approve just moved forward.
        assert [s.stage_name for s in stages if s.kind != "system"] == ["requirements", "architecture"]

        # Architecture also reports low confidence -> pauses again.
        await _complete_skill_stage(
            org_id, run_id, db_session, artifact_body="# Architecture v1", confidence=15
        )
        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "paused"
        pause2 = (
            await db_session.execute(
                select(RunPauseRow).where(RunPauseRow.run_id == run_id, RunPauseRow.resolved_at.is_(None))
            )
        ).scalar_one()

        async with org_context(org_id, ActorKind.SYSTEM):
            await resolve_pause(
                pause2.id,
                resolution=PauseResolution(action="instruct", instruction="cover the multi-region case too"),
                actor=Actor.user(user_id=requester_id),
                session=db_session,
            )
        await db_session.commit()

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "running"
        stages = await _stage_rows(db_session, run_id)
        architecture_rows = [s for s in stages if s.stage_name == "architecture"]
        assert len(architecture_rows) == 2
        instructed_row = architecture_rows[1]
        assert instructed_row.revision is not None
        assert instructed_row.revision["source"] == "instruction"
        assert instructed_row.revision["text"] == "cover the multi-region case too"
        assert instructed_row.revision["prior_artifact"] == "# Architecture v1"


@pytest.mark.asyncio
async def test_dev_pipeline_main_skill_send_back_to_requirements_service(db_session) -> None:
    """`implement`'s own `SkillReturn.outcome="send_back"` (not a review
    residual) rewinds straight to `requirements` — resolvable because
    `requirements` is an upstream `SkillStage` in the flattened `dev`
    definition. The run then re-runs FORWARD through architecture and plan
    before reaching `implement` again."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    with register_stub_vcs(plugin_id="github"):
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
        org_id = org.org_id
        ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
        agent_row = await seed_agent(org_id=org_id)

        _dev_id, run_id = await _start_dev_run(
            db_session, org_id, ticket_id=ticket_id, requester_id=requester_id
        )
        await _complete_provision(org_id, run_id, agent_row["id"], db_session)

        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Requirements v1")
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Architecture v1")
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Plan v1")

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        implement_command_id = run.pending_agent_command_id
        assert implement_command_id is not None
        send_back_output = json.dumps(
            {
                "outcome": "send_back",
                "outcome_reason": "the spec never says how to handle a burst above the bucket size",
                "send_back_to_stage": "requirements",
                "confidence": 60,
                "paths_affected": [],
                "summary": "blocked on a spec gap",
            }
        )
        await _record(
            org_id,
            _success_event(implement_command_id, outputs={"stdout": send_back_output, "exit_code": 0}),
            agent_id=None,
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "running"
        assert run.current_stage_index == 0

        stages = await _stage_rows(db_session, run_id)
        implement_row = next(s for s in stages if s.stage_name == "implement")
        assert implement_row.boundary_outcome == "sent_back"
        requirements_rows = [s for s in stages if s.stage_name == "requirements"]
        assert len(requirements_rows) == 2
        rewound_row = requirements_rows[1]
        assert rewound_row.revision is not None
        assert rewound_row.revision["source"] == "send_back"
        assert "burst" in rewound_row.revision["text"]
        assert rewound_row.revision["prior_artifact"] == "# Requirements v1"

        # Re-runs forward through architecture and plan before implement again.
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Requirements v2")
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Architecture v2")
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Plan v2")

        stages = await _stage_rows(db_session, run_id)
        # Plan v2 completing dispatches `implement` fresh (a new row, not yet
        # terminal) — the run genuinely re-runs forward through the whole
        # tail, not just to the point it failed the first time.
        assert [s.stage_name for s in stages if s.kind != "system"] == [
            "requirements",
            "architecture",
            "plan",
            "implement",
            "requirements",
            "architecture",
            "plan",
            "implement",
        ]


@pytest.mark.asyncio
async def test_dev_pipeline_rerun_from_plan_on_completed_run_service(github_org: UUID, db_session) -> None:
    """`start_rerun_from_stage` on a completed `dev` run starts a NEW run at
    `plan`'s index, inheriting `requirements`/`architecture` by read-through
    (no rows for them on the new run) and threading the instruction as
    `plan`'s first-dispatch revision. Needs the live fake-github subprocess
    (not `register_stub_vcs`) — the FIRST run's `implementation` tail really
    does reach `github:create_pr`, which calls the real `GitHubPlugin`."""
    org_id = github_org
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
    agent_row = await seed_agent(org_id=org_id)

    _dev_id, run_id = await _start_dev_run(db_session, org_id, ticket_id=ticket_id, requester_id=requester_id)
    await _complete_provision(org_id, run_id, agent_row["id"], db_session)
    await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Requirements v1")
    await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Architecture v1")
    await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Plan v1")
    await _complete_skill_stage(
        org_id, run_id, db_session, artifact_body="# Implementation notes", paths_affected=[]
    )
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    review_command_id = run.pending_agent_command_id
    assert review_command_id is not None
    no_findings_review = json.dumps(
        {"new_findings": [], "prior_finding_verdicts": [], "confidence": 95, "summary": "clean"}
    )
    await _record(
        org_id,
        _success_event(review_command_id, outputs={"stdout": no_findings_review, "exit_code": 0}),
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

    with pytest.raises(StageNotInDefinitionError):
        async with org_context(org_id, ActorKind.SYSTEM):
            await start_rerun_from_stage(
                org_id=org_id,
                ticket_id=ticket_id,
                from_stage="does-not-exist",
                instruction="redo it",
                actor=Actor.user(user_id=requester_id),
                session=db_session,
            )

    async with org_context(org_id, ActorKind.SYSTEM):
        rerun_id = await start_rerun_from_stage(
            org_id=org_id,
            ticket_id=ticket_id,
            from_stage="plan",
            instruction="split the plan into a migration step first",
            actor=Actor.user(user_id=requester_id),
            session=db_session,
        )
    await db_session.commit()
    assert rerun_id != run_id

    rerun = await db_session.get(PipelineRunRow, rerun_id)
    assert rerun is not None
    assert rerun.current_stage_index == 2  # plan's index in the flattened dev definition

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

    rerun_stages = await _stage_rows(db_session, rerun_id)
    assert [s.stage_name for s in rerun_stages] == ["provision-workspace", "plan"]
    plan_row = next(s for s in rerun_stages if s.stage_name == "plan")
    assert plan_row.revision is not None
    assert plan_row.revision["source"] == "instruction"
    assert plan_row.revision["text"] == "split the plan into a migration step first"
    assert plan_row.revision["prior_artifact"] == "# Plan v1"


@pytest.mark.asyncio
async def test_dev_pipeline_cancel_while_running_defers_to_next_boundary_service(db_session) -> None:
    """`request_cancel` on a `running` dev run doesn't interrupt the
    in-flight stage; the NEXT boundary check routes to `cancelled` instead
    of dispatching the following stage."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    with register_stub_vcs(plugin_id="github"):
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
        org_id = org.org_id
        ticket_id, requester_id = await _seed_ticket_and_user(db_session, org_id)
        agent_row = await seed_agent(org_id=org_id)

        _dev_id, run_id = await _start_dev_run(
            db_session, org_id, ticket_id=ticket_id, requester_id=requester_id
        )
        await _complete_provision(org_id, run_id, agent_row["id"], db_session)
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Requirements v1")

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "running"

        async with org_context(org_id, ActorKind.SYSTEM):
            await request_cancel(run_id, actor=Actor.user(user_id=requester_id), session=db_session)
        await db_session.commit()

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "running"  # still running — cancel defers to the next boundary
        assert run.cancel_requested is True

        # Architecture completes normally; the boundary check now sees
        # `cancel_requested` and routes to cleanup/cancelled instead of
        # dispatching `plan`.
        await _complete_skill_stage(org_id, run_id, db_session, artifact_body="# Architecture v1")

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
        assert run.state == "cancelled"

        stages = await _stage_rows(db_session, run_id)
        # `plan` never dispatched.
        assert [s.stage_name for s in stages if s.kind != "system"] == ["requirements", "architecture"]
