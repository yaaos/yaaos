"""Service test: the comment feedback loop, driven from `handle_pr_comment`
(the entry point `plugins/github` calls) against a live `apps/fake-github`
subprocess.

Acceptance matrix:
- `@yaaos re-review` starts a run (grammar, no fake-github needed).
- `@yaaos cancel` cancels the ticket's current run (grammar).
- A free-text `question` reply is classified, claimed, and answered — the
  comment-response run's `reply_to_comment` action posts the reply into the
  finding's own thread on fake-github.
- A `claims_fixed` reply arriving while a batch run is already in flight
  stays waiting (claim semantics: mid-run comments don't join the running
  batch) and joins the NEXT batch once that run terminates
  (`AFTER_RUN_TERMINAL` -> `maybe_start_batch_run`).
- A `dispute` reply with a `None` verdict + a reply defends the finding
  (`findings.mark_defended`) — the two-round insist-then-dismiss policy has
  its own dedicated test, `test_defense_policy_service.py`.
- An unanchored comment (no `in_reply_to_external_id`) is always `unclear`
  regardless of content, gets the canned clarification reply, and never
  joins any batch (claim semantics: `unclear` never enters the waiting set).

Uses a locally-defined `_drain` outbox-dispatch helper — the same shape as
`domain/pipelines/test/drain.py`, which is intra-module-only and not
importable from this module's own test directory (see
`apps/backend/docs/patterns.md` § Module boundaries in tests).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.agent_gateway import AgentEvent, AgentEventKind, claim_next, record_agent_event
from app.core.agent_gateway import Artifact as WireArtifact
from app.core.audit_log import Actor, ActorKind
from app.core.auth import Role, org_context
from app.core.config import get_settings
from app.core.identity import create_user
from app.core.tasks import drain_once, get_broker, get_pending_task_names
from app.core.tenancy import create_membership, create_org
from app.core.vcs import fetch_pr, list_yaaos_comments
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.findings import get as get_finding
from app.domain.findings import list_open_for_ticket
from app.domain.orgs import create_org as create_full_org
from app.domain.pipelines import (
    ActionStage,
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    ReviewSkillStage,
    SkillStage,
    create_pipeline,
    get_run_overview,
    has_run_in_flight,
    start_run,
)
from app.domain.pr_review import InboundComment, handle_pr_comment
from app.domain.pr_review.models import PRCommentRow
from app.domain.repos import TriggerBindingSpec, add_binding
from app.domain.tickets import attach_pr_to_ticket, create_from_pr
from app.domain.tickets import upsert as upsert_pull_request
from app.plugins.github import GitHubPlugin, record_app_install
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REPO_EXTERNAL_ID = "acme/web"
_PR_EXTERNAL_ID = "acme/web#1"  # apps/fake-github's default-seeded PR


async def _drain(db_session: Any, *, max_iters: int = 50) -> None:
    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None, f"no task body for {payload['task_name']}"
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


@pytest.fixture
async def github_org(db_session, monkeypatch: pytest.MonkeyPatch, fake_github_base_url: str) -> UUID:
    """An org with an active GitHub App installation pointed at a live
    `apps/fake-github` subprocess — mirrors
    `domain/pipelines/test/test_pr_actions_service.py`'s `github_org`."""
    org = await create_full_org(
        db_session, slug=f"comment-loop-org-{uuid4().hex[:8]}", display_name="Comment Loop Org"
    )
    await record_app_install(
        db_session, org_id=org.id, install_external_id="install-fake-1", account_login="acme"
    )
    await db_session.flush()

    monkeypatch.setenv("YAAOS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("YAAOS_GITHUB_APP_PRIVATE_KEY", "fake-pem-not-a-real-key")
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "whsec-test")
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos-test")
    monkeypatch.setenv("YAAOS_PR_COMMENT_CLASSIFIER_STUB", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(GitHubPlugin, "base_url", fake_github_base_url)

    return org.id


async def _seed_pr_ticket(org_id: UUID, db_session) -> UUID:
    wire_pr = await fetch_pr("github", org_id, _PR_EXTERNAL_ID)
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=_PR_EXTERNAL_ID,
        title=wire_pr.title,
        description=wire_pr.body,
        repo_external_id=_REPO_EXTERNAL_ID,
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        branch_name=wire_pr.head_branch,
        session=db_session,
    )
    pr_row = await upsert_pull_request(wire_pr, ticket_id=ticket_id, org_id=org_id, session=db_session)
    await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=pr_row.id, session=db_session)
    await db_session.flush()
    return ticket_id


def _update_pr_pipeline() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pr-review-{uuid4().hex[:8]}",
        stages=(
            ReviewSkillStage(
                name="code-review",
                skill_name="code-review",
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                finding_prefix="SPEC",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
            ActionStage(description="post findings", action_id="github:update_pr"),
        ),
    )


def _comment_response_pipeline() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"comment-response-{uuid4().hex[:8]}",
        stages=(
            ReviewSkillStage(
                name="respond",
                skill_name="respond",
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
            ActionStage(description="reply to comments", action_id="github:reply_to_comment"),
        ),
    )


async def _record(org_id: UUID, event: AgentEvent, *, agent_id: UUID | None, db_session) -> None:
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()


async def _respond(
    org_id: UUID, cmd, *, outputs: dict, agent_id: UUID | None, db_session, artifact_body: str | None = None
) -> None:
    """Report success for a command claimed via `claim_next` — echoes its
    `completion_token` back, exactly like the real agent does (`record_agent_event`
    verifies it against the claim's stored hash)."""
    event = AgentEvent(
        command_id=cmd.command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs=outputs,
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=cmd.completion_token,
        artifact=WireArtifact(body=artifact_body) if artifact_body is not None else None,
    )
    await _record(org_id, event, agent_id=agent_id, db_session=db_session)


async def _drive_next_run(org_id: UUID, agent_id: UUID, *, review_output: dict, db_session) -> None:
    """Claim + respond to the next run's provision -> review -> cleanup
    commands, via the same public claim surface (`core.agent_gateway.claim_next`)
    the real agent uses — never touches `domain/pipelines` internals
    directly (this test lives in a different module's test directory; see
    `apps/backend/docs/patterns.md` § Module boundaries in tests). The
    action stage runs synchronously inside the engine's own handling of the
    review's terminal event, so no separate claim is needed for it."""
    await _drain(db_session)
    provision_cmd = await claim_next(
        agent_id, lifecycle="active", new_workspaces=1, workspace_ids=[], wait_seconds=0, session=db_session
    )
    assert provision_cmd is not None
    workspace_id = provision_cmd.workspace_id
    await _respond(org_id, provision_cmd, outputs={}, agent_id=agent_id, db_session=db_session)
    await _drain(db_session)

    review_cmd = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[workspace_id],
        wait_seconds=0,
        session=db_session,
    )
    assert review_cmd is not None
    await _respond(
        org_id,
        review_cmd,
        outputs={"stdout": json.dumps(review_output), "exit_code": 0},
        agent_id=None,
        db_session=db_session,
    )
    await _drain(db_session)  # runs the action stage synchronously, then dispatches cleanup

    cleanup_cmd = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[workspace_id],
        wait_seconds=0,
        session=db_session,
    )
    assert cleanup_cmd is not None
    await _respond(org_id, cleanup_cmd, outputs={}, agent_id=None, db_session=db_session)
    await _drain(db_session)


async def _comment_row(db_session, *, prefix: str) -> PRCommentRow:
    rows = (
        (await db_session.execute(select(PRCommentRow).where(PRCommentRow.author_login == "dev1")))
        .scalars()
        .all()
    )
    return next(r for r in rows if r.body.startswith(prefix))


# ── Grammar (no fake-github needed) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_re_review_comment_starts_run_service(db_session) -> None:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Grammar Org")
    org_id = org.org_id
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="grammar test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        branch_name="yaaos/test-branch",
        session=db_session,
    )
    await db_session.flush()

    # A single-action pipeline; never drained in this test, so the action
    # never actually executes — `start_run`'s own synchronous `queued ->
    # running` promotion is all this test needs.
    pipeline_def = PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(ActionStage(description="reply-to-comment noop", action_id="github:reply_to_comment"),),
    )
    pipeline_id = await create_pipeline(
        org_id=org_id, definition=pipeline_def, actor=Actor.system(), session=db_session
    )
    await db_session.flush()
    await add_binding(
        org_id,
        "acme/repo",
        spec=TriggerBindingSpec(intake_point_id="github:pr_opened", pipeline_id=pipeline_id),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    assert not await has_run_in_flight(ticket_id, session=db_session)

    await handle_pr_comment(
        org_id=org_id,
        ticket_id=ticket_id,
        comment=InboundComment(external_id="c1", author_login="alice", body="@yaaos re-review"),
        session=db_session,
    )
    await db_session.commit()

    assert await has_run_in_flight(ticket_id, session=db_session)


@pytest.mark.asyncio
async def test_cancel_comment_cancels_current_run_service(db_session) -> None:
    with register_stub_vcs(plugin_id="github"):
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Cancel Org")
        org_id = org.org_id
        user = await create_user(db_session, display_name="Watcher")
        await create_membership(
            db_session, user_id=user.id, org_id=org_id, role=Role.BUILDER, handle="watcher"
        )
        ticket_id, _ = await create_from_pr(
            org_id=org_id,
            source_external_id=f"ext-{uuid4().hex[:8]}",
            title="cancel test ticket",
            description=None,
            repo_external_id="acme/repo",
            plugin_id="github",
            idempotency_key=f"key-{uuid4().hex}",
            payload={},
            branch_name="yaaos/test-branch",
            session=db_session,
        )
        await db_session.flush()

        if not is_workspace_provider_registered("remote_agent"):
            register_workspace_providers()
        agent_row = await seed_agent(org_id=org_id)

        pipeline_def = PipelineDefinition(
            name=f"pipe-{uuid4().hex[:8]}",
            stages=(
                SkillStage(
                    name="implement",
                    skill_name="implement",
                    coding_agent_plugin_id="claude_code",
                    model="sonnet",
                    effort="medium",
                    boundary=BoundaryControl(mode="always_proceed"),
                ),
            ),
        )
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=pipeline_def, actor=Actor.system(), session=db_session
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user.id), input_text="do work")
        await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await _drain(db_session)

        agent_id = agent_row["id"]
        provision_cmd = await claim_next(
            agent_id,
            lifecycle="active",
            new_workspaces=1,
            workspace_ids=[],
            wait_seconds=0,
            session=db_session,
        )
        assert provision_cmd is not None
        workspace_id = provision_cmd.workspace_id
        await _respond(org_id, provision_cmd, outputs={}, agent_id=agent_id, db_session=db_session)
        await _drain(db_session)

        # The main skill dispatch is now in flight (claimable) — the run is
        # genuinely `running`, not just promoted.
        main_cmd = await claim_next(
            agent_id,
            lifecycle="active",
            new_workspaces=0,
            workspace_ids=[workspace_id],
            wait_seconds=0,
            session=db_session,
        )
        assert main_cmd is not None

        await handle_pr_comment(
            org_id=org_id,
            ticket_id=ticket_id,
            comment=InboundComment(external_id="c-cancel", author_login="alice", body="@yaaos cancel"),
            session=db_session,
        )
        await db_session.commit()

        # Cancel doesn't preempt the in-flight command — it takes effect at
        # the next boundary. Complete the main dispatch + cleanup and prove
        # the run lands `cancelled`, not `completed`.
        await _respond(
            org_id,
            main_cmd,
            outputs={
                "stdout": json.dumps(
                    {"outcome": "completed", "confidence": 90, "paths_affected": [], "summary": "done"}
                ),
                "exit_code": 0,
            },
            agent_id=None,
            db_session=db_session,
            artifact_body="# work done",
        )
        await _drain(db_session)

        cleanup_cmd = await claim_next(
            agent_id,
            lifecycle="active",
            new_workspaces=0,
            workspace_ids=[workspace_id],
            wait_seconds=0,
            session=db_session,
        )
        assert cleanup_cmd is not None
        await _respond(org_id, cleanup_cmd, outputs={}, agent_id=None, db_session=db_session)
        await _drain(db_session)

        async with org_context(org_id, ActorKind.SYSTEM, actor_id=None):
            overview = await get_run_overview(ticket_id, session=db_session)
        assert overview is not None
        assert overview.status == "terminal"
        assert overview.outcome is not None
        assert overview.outcome.state == "cancelled"


# ── Classification + batching + reply-posting (needs live fake-github) ──


@pytest.mark.asyncio
async def test_classification_batching_and_reply_acceptance(
    github_org: UUID, fake_github_base_url: str, db_session
) -> None:
    org_id = github_org
    ticket_id = await _seed_pr_ticket(org_id, db_session)

    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]

    # First run posts two findings to the real PR (blocker + nit), anchored
    # via `findings.set_external_anchor` — real comment ids to reply under.
    update_pipeline_id = await create_pipeline(
        org_id=org_id, definition=_update_pr_pipeline(), actor=Actor.system(), session=db_session
    )
    await db_session.flush()
    wire_pr = await fetch_pr("github", org_id, _PR_EXTERNAL_ID)
    kickoff = Kickoff(
        intake_point_id="github:pr_opened",
        actor=Actor.system(),
        input_text=None,
        pr_head_sha=wire_pr.head_sha,
        pr_base_sha=wire_pr.base_sha,
    )
    await start_run(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=update_pipeline_id,
        kickoff=kickoff,
        session=db_session,
    )
    await db_session.commit()
    await _drive_next_run(
        org_id,
        agent_id,
        review_output={
            "new_findings": [
                {"severity": "blocker", "body": "SQL injection risk", "code_file": "app.py", "code_line": 10},
                {"severity": "nit", "body": "naming nit"},
            ],
            "prior_finding_verdicts": [],
            "confidence": 80,
            "summary": "found issues",
        },
        db_session=db_session,
    )

    findings = await list_open_for_ticket(org_id, ticket_id, session=db_session)
    spec001 = next(f for f in findings if f.severity == "blocker")
    spec002 = next(f for f in findings if f.severity == "nit")
    assert spec001.external_comment_id is not None
    assert spec002.external_comment_id is not None

    # Bind the comment-response pipeline.
    comment_pipeline_id = await create_pipeline(
        org_id=org_id, definition=_comment_response_pipeline(), actor=Actor.system(), session=db_session
    )
    await db_session.flush()
    await add_binding(
        org_id,
        _REPO_EXTERNAL_ID,
        spec=TriggerBindingSpec(intake_point_id="github:pr_comment", pipeline_id=comment_pipeline_id),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    # C1: question, anchored on spec001. Classified + immediately batched
    # (no run in flight yet).
    await handle_pr_comment(
        org_id=org_id,
        ticket_id=ticket_id,
        comment=InboundComment(
            external_id=f"ext-{uuid4().hex[:8]}",
            author_login="dev1",
            body="why is this a problem?",
            in_reply_to_external_id=spec001.external_comment_id,
        ),
        session=db_session,
    )
    await db_session.commit()

    # C3: unanchored — always `unclear`, regardless of content, and gets its
    # canned reply without ever entering the waiting set.
    await handle_pr_comment(
        org_id=org_id,
        ticket_id=ticket_id,
        comment=InboundComment(external_id=f"ext-{uuid4().hex[:8]}", author_login="dev1", body="thanks!"),
        session=db_session,
    )
    await db_session.commit()

    await _drain(db_session)  # classifies C1 + C3; C1 starts batch run B1; C3 replies immediately

    c1_row = await _comment_row(db_session, prefix="why")
    c3_row = await _comment_row(db_session, prefix="thanks")
    assert c1_row.classification == "question"
    assert c1_row.claimed_by_run_id is not None
    b1_run_id = c1_row.claimed_by_run_id
    assert c3_row.classification == "unclear"
    assert c3_row.claimed_by_run_id is None

    comments_on_pr = await list_yaaos_comments("github", org_id, _PR_EXTERNAL_ID)
    assert any("Not sure what you're asking" in c.body for c in comments_on_pr)

    # C2: claims_fixed on spec001, and C4: dispute on spec002 — both arrive
    # while B1 is in flight, so both stay waiting (mid-run wait).
    await handle_pr_comment(
        org_id=org_id,
        ticket_id=ticket_id,
        comment=InboundComment(
            external_id=f"ext-{uuid4().hex[:8]}",
            author_login="dev1",
            body="fixed in 1a2b3c4",
            in_reply_to_external_id=spec001.external_comment_id,
        ),
        session=db_session,
    )
    await db_session.commit()
    await handle_pr_comment(
        org_id=org_id,
        ticket_id=ticket_id,
        comment=InboundComment(
            external_id=f"ext-{uuid4().hex[:8]}",
            author_login="dev1",
            body="I disagree, this isn't a bug",
            in_reply_to_external_id=spec002.external_comment_id,
        ),
        session=db_session,
    )
    await db_session.commit()
    await _drain(db_session)

    c2_row = await _comment_row(db_session, prefix="fixed")
    c4_row = await _comment_row(db_session, prefix="I disagree")
    assert c2_row.classification == "claims_fixed"
    assert c2_row.claimed_by_run_id is None  # mid-run wait — B1 still in flight
    assert c4_row.classification == "dispute"
    assert c4_row.claimed_by_run_id is None

    # Complete B1 — the question gets answered.
    await _drive_next_run(
        org_id,
        agent_id,
        review_output={
            "new_findings": [],
            "prior_finding_verdicts": [
                {
                    "finding_id": str(spec001.id),
                    "status": None,
                    "reply": "Because it lets an attacker inject SQL.",
                }
            ],
            "confidence": 85,
            "summary": "answered the question",
        },
        db_session=db_session,
    )
    comments_on_pr = await list_yaaos_comments("github", org_id, _PR_EXTERNAL_ID)
    assert any("inject SQL" in c.body for c in comments_on_pr)

    # AFTER_RUN_TERMINAL fired at B1's completion; drain it so
    # `maybe_start_batch_run` claims the now-waiting C2 + C4 into B2.
    await _drain(db_session)
    c2_row = await _comment_row(db_session, prefix="fixed")
    c4_row = await _comment_row(db_session, prefix="I disagree")
    assert c2_row.claimed_by_run_id is not None
    assert c2_row.claimed_by_run_id == c4_row.claimed_by_run_id
    assert c2_row.claimed_by_run_id != b1_run_id  # a fresh batch run, not B1

    await _drive_next_run(
        org_id,
        agent_id,
        review_output={
            "new_findings": [],
            "prior_finding_verdicts": [
                {
                    "finding_id": str(spec001.id),
                    "status": None,
                    "reply": "Fix looks good — will re-verify on your next push.",
                },
                {
                    "finding_id": str(spec002.id),
                    "status": None,
                    "reply": "I hear you — this only matters in one edge case.",
                },
            ],
            "confidence": 85,
            "summary": "answered comments",
        },
        db_session=db_session,
    )

    comments_on_pr = await list_yaaos_comments("github", org_id, _PR_EXTERNAL_ID)
    assert any("will re-verify" in c.body for c in comments_on_pr)
    assert any("only matters in one edge case" in c.body for c in comments_on_pr)

    defended_spec002 = await get_finding(spec002.id, session=db_session)
    assert defended_spec002.defended_at is not None
