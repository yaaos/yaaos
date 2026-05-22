"""`PostReply` — appends a yaaos reply to a finding's comment thread.

Covers the defensive branches. Happy-path (reply persists to thread with
correct author_kind) requires building a real aggregate fixture with a
finding + thread; that's exercised end-to-end via the answer_question_v1
workflow once the AnswerQuestion body lands. The wrapper logic here is
what we verify.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    _reset_workflow_context_provider_for_tests,
    register_workflow_context_provider,
)
from app.domain.pull_requests.models import PullRequestRow
from app.domain.reviewer.admission import (
    admit_raw_findings,
    post_admitted_findings_to_vcs,
)
from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.commands import PostReply
from app.domain.reviewer.models import CommentMessageRow
from app.domain.reviewer.types import CodeAnchor, FindingFingerprint
from app.domain.tickets.models import TicketRow
from app.testing.stub_vcs import register_stub_vcs


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="reply",
        attempt=0,
    )


class _StaticProvider:
    def __init__(self, context: WorkspaceTicketContext | None) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


async def test_empty_inputs_is_noop_success() -> None:
    _reset_workflow_context_provider_for_tests()
    outcome = await PostReply().execute({}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("posted") is False
    assert outcome.outputs.get("reason") == "empty_input"


async def test_empty_reply_body_is_noop() -> None:
    _reset_workflow_context_provider_for_tests()
    outcome = await PostReply().execute({"reply_body": "", "finding_id": str(uuid4())}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("posted") is False


async def test_invalid_finding_id_returns_failure() -> None:
    _reset_workflow_context_provider_for_tests()
    outcome = await PostReply().execute({"reply_body": "looks good", "finding_id": "not-a-uuid"}, _ctx())
    assert outcome.label == "failure"
    assert "invalid finding_id" in (outcome.failure_reason or "")


async def test_no_provider_registered_returns_failure() -> None:
    _reset_workflow_context_provider_for_tests()
    outcome = await PostReply().execute({"reply_body": "looks good", "finding_id": str(uuid4())}, _ctx())
    assert outcome.label == "failure"
    assert "no workflow_context provider" in (outcome.failure_reason or "")


async def test_no_pr_link_is_noop_success() -> None:
    _reset_workflow_context_provider_for_tests()
    register_workflow_context_provider(
        _StaticProvider(
            context=WorkspaceTicketContext(
                org_id=uuid4(),
                plugin_id="github",
                repo_external_id="me/repo",
                payload={},
                pr_id=None,
            )
        )
    )
    outcome = await PostReply().execute({"reply_body": "looks good", "finding_id": str(uuid4())}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("posted") is False
    assert outcome.outputs.get("reason") == "no_pr_link"


async def test_unknown_finding_is_noop_success(db_session) -> None:  # type: ignore[no-untyped-def]
    """pr_id present but the finding_id isn't in the aggregate. Success-no-op
    so the workflow drains."""
    _reset_workflow_context_provider_for_tests()
    register_workflow_context_provider(
        _StaticProvider(
            context=WorkspaceTicketContext(
                org_id=uuid4(),
                plugin_id="github",
                repo_external_id="me/repo",
                payload={},
                pr_id=uuid4(),
            )
        )
    )
    _ = db_session
    outcome = await PostReply().execute({"reply_body": "looks good", "finding_id": str(uuid4())}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("posted") is False
    assert outcome.outputs.get("reason") == "unknown_finding"


# ── Happy path: real GitHub-side post via stub vcs ─────────────────────


async def test_post_reply_calls_vcs_when_real_parent_exists(db_session) -> None:  # type: ignore[no-untyped-def]
    """When the thread has a real (non-local) parent yaaos comment AND a
    PR row exists, PostReply calls vcs.post_comment_reply and persists the
    real external_comment_id (not the local-reply placeholder).
    """
    _reset_workflow_context_provider_for_tests()
    org_id = uuid4()

    # 1. Seed ticket + PR rows.
    ticket_id = uuid4()
    db_session.add(
        TicketRow(
            id=ticket_id,
            org_id=org_id,
            source="github_pr",
            source_external_id="42",
            title="t",
            status="pending",
            plugin_id="github",
            repo_external_id="me/repo",
            type="github_pr",
            idempotency_key=f"reply-{uuid4()}",
            payload={},
        )
    )
    await db_session.flush()
    pr_id = uuid4()
    db_session.add(
        PullRequestRow(
            id=pr_id,
            org_id=org_id,
            plugin_id="github",
            external_id="pr-x",
            repo_external_id="me/repo",
            ticket_id=ticket_id,
            number=42,
            title="t",
            body=None,
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feature",
            base_sha="b",
            head_sha="h",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="http://test",
        )
    )
    await db_session.commit()

    # 2. Admit one finding + post via vcs to create a real parent comment.
    raw = [
        RawFinding(
            fingerprint=FindingFingerprint(
                file_path="src/foo.py",
                rule_id="r1",
                anchor_content_hash="a",
                body_gist_hash="b",
            ),
            rule_id="r1",
            title="t",
            body="b",
            rationale="r",
            concrete_failure_scenario="Long enough concrete failure scenario for the schema gate.",
            confidence=90,
            severity="major",
            anchor=CodeAnchor(
                file_path="src/foo.py",
                line_start=1,
                line_end=1,
                surrounding_content_hash="s",
                commit_sha="h",
            ),
            source_agent="t",
        )
    ]
    with register_stub_vcs(plugin_id="github"):
        admission = await admit_raw_findings(
            pr_id=pr_id,
            org_id=org_id,
            raw=raw,
            commit_sha="h",
            session=db_session,
        )
        assert len(admission.admitted) == 1
        finding_id = admission.admitted[0].id

        await post_admitted_findings_to_vcs(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id="pr-x",
            vcs_plugin_id="github",
            admitted=admission.admitted,
            raw=raw,
            summary_body=None,
            session=db_session,
        )
        await db_session.commit()

        # 3. Register provider so PostReply can find the ticket context.
        register_workflow_context_provider(
            _StaticProvider(
                context=WorkspaceTicketContext(
                    org_id=org_id,
                    plugin_id="github",
                    repo_external_id="me/repo",
                    payload={},
                    pr_id=pr_id,
                )
            )
        )

        # 4. Call PostReply.
        outcome = await PostReply().execute(
            {"reply_body": "ack, fixing", "finding_id": str(finding_id)}, _ctx()
        )

    assert outcome.label == "success"
    assert outcome.outputs.get("posted") is True

    # 5. Stub recorded a post_comment_reply (per the stub's signature
    #    it returns "stub-reply-comment-id"); the new CommentMessageRow
    #    has that external_comment_id.
    msgs = (
        (
            await db_session.execute(
                select(CommentMessageRow).where(
                    CommentMessageRow.external_comment_id == "stub-reply-comment-id"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    assert msgs[0].author_kind == "yaaos"
    assert msgs[0].body == "ack, fixing"
