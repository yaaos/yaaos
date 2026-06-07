"""`PostFindings` — parses agent stdout and persists `ReportedFinding`s.

Covers the defensive branches that don't require a workspace + full
DB fixture. Happy-path (stdout → FindingRow via publish_findings) rides on
`test_post_findings_happy_path.py`; this slice verifies the edge cases.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.workflow import CommandContext
from app.domain.reviewer.commands import PostFindings


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="post",
        attempt=0,
    )


async def test_empty_stdout_returns_success_zero_count(workflow_context_provider_isolation) -> None:  # type: ignore[no-untyped-def]
    """No stdout → zero findings, success without calling the DB."""
    outcome = await PostFindings().execute({}, _ctx())
    assert outcome.label == "success"
    assert outcome.outputs.get("admitted_count") == 0


async def test_nonconforming_stdout_returns_schema_invalid_failure() -> None:
    """Stdout that contains no terminal result event → schema_invalid failure."""
    outcome = await PostFindings().execute(
        {"stdout": "not valid json stream output"},
        _ctx(),
    )
    assert outcome.label == "failure"
    assert outcome.failure_reason == "schema_invalid"
