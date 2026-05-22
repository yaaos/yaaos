"""`PostFindings` — persists coding-agent findings through admission.

Covers the defensive branches that don't require a workspace + aggregate
fixture. Happy-path (FindingDraft → RawFinding → admit) rides on the
existing test_aggregate.py + test_admission.py coverage; this slice
verifies the wrapper plumbing.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    _reset_providers_for_tests,
    _reset_workflow_context_provider_for_tests,
)
from app.domain.reviewer.commands import PostFindings


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="post",
        attempt=0,
    )


class _StaticProvider:
    def __init__(self, context: WorkspaceTicketContext | None) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


async def test_empty_drafts_returns_zero_counts() -> None:
    """Stubbed CodeReview (still the default behavior) emits no drafts →
    PostFindings drains the workflow with success-no-op."""
    _reset_workflow_context_provider_for_tests()
    outcome = await PostFindings().execute({}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("admitted_count") == 0
    assert outcome.outputs.get("dropped_count") == 0


async def test_drafts_without_workspace_id_returns_failure() -> None:
    """If upstream produced drafts but workspace_id is missing, we can't
    build stable fingerprints — fail loudly."""
    _reset_workflow_context_provider_for_tests()
    outcome = await PostFindings().execute(
        {"draft_findings": [{"rule_id": "r1"}]},
        _ctx(),
    )
    assert outcome.label == "failure"
    assert "missing workspace_id" in (outcome.failure_reason or "")


async def test_drafts_with_invalid_workspace_id_returns_failure() -> None:
    _reset_workflow_context_provider_for_tests()
    outcome = await PostFindings().execute(
        {
            "draft_findings": [{"rule_id": "r1"}],
            "workspace_id": "not-a-uuid",
        },
        _ctx(),
    )
    assert outcome.label == "failure"
    assert "invalid workspace_id" in (outcome.failure_reason or "")


async def test_drafts_unresolvable_workspace_returns_failure(db_session) -> None:  # type: ignore[no-untyped-def]
    """workspace_id parses but the row doesn't exist."""
    _reset_providers_for_tests()
    _reset_workflow_context_provider_for_tests()
    _ = db_session  # ensure schema migrated
    outcome = await PostFindings().execute(
        {
            "draft_findings": [{"rule_id": "r1"}],
            "workspace_id": str(uuid4()),
        },
        _ctx(),
    )
    assert outcome.label == "failure"
    assert "not resolvable" in (outcome.failure_reason or "")
