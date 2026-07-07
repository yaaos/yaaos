"""Service test: the deterministic dispute policy — defend once, then a
second dispute on the same finding forces a dismiss regardless of what the
skill now asserts (`plugins.github.actions.GitHubReplyToCommentAction`).

Uses `app.testing.stub_vcs` (not a live `apps/fake-github` subprocess) —
the Acceptance sentence for this scenario doesn't require verifying against
a real GitHub API, only the durable finding state + the recorded reply
call; `test_comment_loop_service.py` covers the live-fake-github posting
path for the other three comment classes.

Uses a locally-defined `_drain` outbox-dispatch helper — the same shape as
`domain/pipelines/test/drain.py`, which is intra-module-only and not
importable from this module's own test directory (see
`apps/backend/docs/patterns.md` § Module boundaries in tests). Drives runs
via `core.agent_gateway.claim_next` — the same public claim surface the
real agent uses — rather than reading `domain/pipelines` internals (also
a cross-module-reach restriction from this test's own directory).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.agent_gateway import AgentEvent, AgentEventKind, claim_next, record_agent_event
from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.config import get_settings
from app.core.tasks import drain_once, get_broker, get_pending_task_names
from app.core.tenancy import create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.findings import get as get_finding
from app.domain.findings import list_open_for_ticket
from app.domain.pipelines import (
    ActionStage,
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    ReviewSkillStage,
    create_pipeline,
    start_run,
)
from app.domain.pr_review import InboundComment, handle_pr_comment
from app.domain.repos import TriggerBindingSpec, add_binding
from app.domain.tickets import attach_pr_to_ticket, create_from_pr
from app.domain.tickets import upsert as upsert_pull_request
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import StubVCSPlugin, register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_REPO_EXTERNAL_ID = "owner/repo"
_PR_EXTERNAL_ID = "owner/repo#1"  # StubVCSPlugin's default PR


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


async def _seed_pr_ticket(org_id: UUID, stub: StubVCSPlugin, db_session) -> UUID:
    wire_pr = await stub.fetch_pr(org_id, _PR_EXTERNAL_ID)
    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=wire_pr.external_id,
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


async def _respond(org_id: UUID, cmd, *, outputs: dict, agent_id: UUID | None, db_session) -> None:
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
    )
    await _record(org_id, event, agent_id=agent_id, db_session=db_session)


async def _drive_next_run(org_id: UUID, agent_id: UUID, *, review_output: dict, db_session) -> None:
    """Claim + respond to the next run's provision -> review -> cleanup
    commands, via the public `core.agent_gateway.claim_next` claim surface.
    The action stage runs synchronously inside the engine's own handling of
    the review's terminal event — no separate claim for it."""
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


@pytest.mark.asyncio
async def test_defend_once_then_insist_forces_dismiss(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YAAOS_PR_COMMENT_CLASSIFIER_STUB", "1")
    get_settings.cache_clear()

    with register_stub_vcs(plugin_id="github") as stub:
        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Defense Policy Org")
        org_id = org.org_id
        ticket_id = await _seed_pr_ticket(org_id, stub, db_session)

        if not is_workspace_provider_registered("remote_agent"):
            register_workspace_providers()
        agent_row = await seed_agent(org_id=org_id)
        agent_id = agent_row["id"]

        # Seed one finding via a real review+update_pr run.
        update_pipeline_id = await create_pipeline(
            org_id=org_id, definition=_update_pr_pipeline(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="github:pr_opened", actor=Actor.system(), input_text=None)
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
                "new_findings": [{"severity": "should_fix", "body": "missing null check"}],
                "prior_finding_verdicts": [],
                "confidence": 80,
                "summary": "found one issue",
            },
            db_session=db_session,
        )

        findings = await list_open_for_ticket(org_id, ticket_id, session=db_session)
        assert len(findings) == 1
        spec001 = findings[0]
        assert spec001.external_comment_id is not None

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

        # Round 1: dispute -> the skill defends without asserting a status
        # (status=None + a reply) -> `findings.mark_defended`.
        await handle_pr_comment(
            org_id=org_id,
            ticket_id=ticket_id,
            comment=InboundComment(
                external_id=f"ext-{uuid4().hex[:8]}",
                author_login="dev1",
                body="I disagree, this isn't a real issue",
                in_reply_to_external_id=spec001.external_comment_id,
            ),
            session=db_session,
        )
        await db_session.commit()

        await _drive_next_run(
            org_id,
            agent_id,
            review_output={
                "new_findings": [],
                "prior_finding_verdicts": [
                    {"finding_id": str(spec001.id), "status": None, "reply": "Understood — leaving as a nit."}
                ],
                "confidence": 80,
                "summary": "defended once",
            },
            db_session=db_session,
        )

        defended = await get_finding(spec001.id, session=db_session)
        assert defended.defended_at is not None
        assert defended.status == "open"
        assert any(
            reply[3] == "Understood — leaving as a nit."
            for reply in stub.posted_replies
            if reply[2] == spec001.external_comment_id
        )

        # Round 2: a second dispute on the same finding. The skill insists
        # it's still valid (`still_present` — the engine reflags it
        # mechanically first) — the action coerces a dismiss anyway since
        # the finding is already defended and the verdict isn't
        # `user_overrode`.
        await handle_pr_comment(
            org_id=org_id,
            ticket_id=ticket_id,
            comment=InboundComment(
                external_id=f"ext-{uuid4().hex[:8]}",
                author_login="dev1",
                body="disagree, still not a bug",
                in_reply_to_external_id=spec001.external_comment_id,
            ),
            session=db_session,
        )
        await db_session.commit()

        await _drive_next_run(
            org_id,
            agent_id,
            review_output={
                "new_findings": [],
                "prior_finding_verdicts": [
                    {
                        "finding_id": str(spec001.id),
                        "status": "still_present",
                        "reply": "Still looks like a bug to me.",
                    }
                ],
                "confidence": 80,
                "summary": "insisted",
            },
            db_session=db_session,
        )

        dismissed = await get_finding(spec001.id, session=db_session)
        assert dismissed.status == "dismissed"
        assert dismissed.status_events[-1].method == "user_overrode"
        assert any(
            reply[2] == spec001.external_comment_id
            for reply in stub.posted_replies
            if reply[3] == "Still looks like a bug to me."
        )
