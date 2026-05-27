"""`ResolveFinding` — applies a verify-fix verdict to a single finding.

Verifies the defensive branches of the wrapper. The aggregate's own
`record_fix_verification` state transition is covered in
`test_aggregate.py`; here we verify the verdict-parsing + provider-lookup
+ unknown-finding paths.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    clear_workflow_context_provider,
    register_workflow_context_provider,
)
from app.domain.reviewer.commands import ResolveFinding


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="resolve",
        attempt=0,
    )


class _StaticProvider:
    def __init__(self, context: WorkspaceTicketContext | None) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


async def test_empty_verdict_is_noop_success() -> None:
    clear_workflow_context_provider()
    outcome = await ResolveFinding().execute({}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("transitioned_to") is None


async def test_missing_finding_id_is_noop_success() -> None:
    clear_workflow_context_provider()
    outcome = await ResolveFinding().execute(
        {"verdict": {"still_present": False, "confidence": 0.95}}, _ctx()
    )
    assert outcome.label == "success"
    assert outcome.outputs.get("transitioned_to") is None


async def test_invalid_finding_id_returns_failure() -> None:
    clear_workflow_context_provider()
    outcome = await ResolveFinding().execute(
        {
            "verdict": {
                "finding_id": "not-a-uuid",
                "still_present": False,
                "confidence": 0.95,
            }
        },
        _ctx(),
    )
    assert outcome.label == "failure"
    assert "invalid finding_id" in (outcome.failure_reason or "")


async def test_invalid_confidence_returns_failure() -> None:
    clear_workflow_context_provider()
    outcome = await ResolveFinding().execute(
        {
            "verdict": {
                "finding_id": str(uuid4()),
                "still_present": False,
                "confidence": "very high",
            }
        },
        _ctx(),
    )
    assert outcome.label == "failure"
    assert "invalid confidence" in (outcome.failure_reason or "")


async def test_no_provider_registered_returns_failure() -> None:
    clear_workflow_context_provider()
    outcome = await ResolveFinding().execute(
        {
            "verdict": {
                "finding_id": str(uuid4()),
                "still_present": False,
                "confidence": 0.95,
            }
        },
        _ctx(),
    )
    assert outcome.label == "failure"
    assert "no workflow_context provider" in (outcome.failure_reason or "")


async def test_no_pr_link_is_noop_success() -> None:
    clear_workflow_context_provider()
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
    outcome = await ResolveFinding().execute(
        {
            "verdict": {
                "finding_id": str(uuid4()),
                "still_present": False,
                "confidence": 0.95,
            }
        },
        _ctx(),
    )
    assert outcome.label == "success"
    assert outcome.outputs.get("transitioned_to") is None


async def test_unknown_finding_is_noop_success(db_session) -> None:  # type: ignore[no-untyped-def]
    """pr_id present but the verdict's finding_id isn't in the aggregate
    (deleted, or stale upstream payload) → success-no-op."""
    clear_workflow_context_provider()
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
    outcome = await ResolveFinding().execute(
        {
            "verdict": {
                "finding_id": str(uuid4()),
                "still_present": False,
                "confidence": 0.95,
            }
        },
        _ctx(),
    )
    assert outcome.label == "success"
    assert outcome.outputs.get("transitioned_to") is None
