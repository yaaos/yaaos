"""`PostFindings` — typed findings list → persist.

Covers the defensive branches that don't require a full DB fixture. The
happy-path (typed `ReportedFindingShape` list → FindingRow via
`publish_findings`) rides on `test_post_findings_happy_path.py`.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.workflow import CommandContext
from app.domain.reviewer.commands import PostFindings, PostFindingsInputs


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="PostFindings",
        attempt=0,
    )


async def test_no_pr_id_returns_success_zero_count(db_session) -> None:  # type: ignore[no-untyped-def]
    """No pr_id → zero findings, success without calling the DB."""
    outcome = await PostFindings().execute(
        PostFindingsInputs(findings=[], org_id=uuid4(), pr_id=None), _ctx(), session=db_session
    )
    assert outcome.label == "success"
    assert outcome.outputs.admitted_count == 0


async def test_empty_findings_list_with_no_pr_returns_success(db_session) -> None:  # type: ignore[no-untyped-def]
    """Empty findings list and no pr_id → success with zero count."""
    outcome = await PostFindings().execute(
        PostFindingsInputs(findings=[], org_id=uuid4()),
        _ctx(),
        session=db_session,
    )
    assert outcome.label == "success"
    assert outcome.outputs.admitted_count == 0
