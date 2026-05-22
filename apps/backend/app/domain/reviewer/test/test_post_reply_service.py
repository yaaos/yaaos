"""`PostReply` — appends a yaaos reply to a finding's comment thread.

Covers the defensive branches. Happy-path (reply persists to thread with
correct author_kind) requires building a real aggregate fixture with a
finding + thread; that's exercised end-to-end via the answer_question_v1
workflow once the AnswerQuestion body lands. The wrapper logic here is
what we verify.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    _reset_workflow_context_provider_for_tests,
    register_workflow_context_provider,
)
from app.domain.reviewer.commands import PostReply


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
