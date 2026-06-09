"""Service tests for findings rollup written by reviewer to the ticket row.

Covers:
- refresh_ticket_findings_summary writes correct count + max severity after review end.
- Findings with no entries produce count=0, max_severity=None.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.domain.reviewer.service import refresh_ticket_findings_summary
from app.domain.tickets import get as get_ticket
from app.domain.tickets import update_findings_summary

# ── shared seed helpers ───────────────────────────────────────────────────────


async def _seed_ticket_and_pr(  # type: ignore[no-untyped-def]
    db_session, *, org_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a ticket + linked PR; return (ticket_id, pr_id)."""
    ticket_id = uuid.uuid4()
    pr_id = uuid.uuid4()
    src_ext = f"acme/repo#{uuid.uuid4().hex[:8]}"
    pr_ext = f"acme/repo#{uuid.uuid4().hex[:8]}"
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id, pr_id)"
            " VALUES (:id, :org_id, 'github_pr', :src_ext, 't', 'running',"
            " 'github', 'acme/repo', :pr_id)"
        ),
        {"id": ticket_id, "org_id": org_id, "src_ext": src_ext, "pr_id": pr_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id, number, title, body,"
            "  author_login, author_type, base_branch, head_branch, base_sha, head_sha,"
            "  is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', :pr_ext, 'acme/repo', 1, 't', '',"
            "         'dev', 'user', 'main', 'feat', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "tid": ticket_id, "pr_ext": pr_ext},
    )
    return ticket_id, pr_id


async def _seed_review(  # type: ignore[no-untyped-def]
    db_session,
    *,
    pr_id: uuid.UUID,
    org_id: uuid.UUID,
    seq: int = 1,
) -> uuid.UUID:
    """Insert a review row; return its id."""
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status)"
            " VALUES (:id, :org_id, :pr_id, :seq, 'done')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id, "seq": seq},
    )
    return review_id


async def _seed_finding(  # type: ignore[no-untyped-def]
    db_session,
    *,
    pr_id: uuid.UUID,
    org_id: uuid.UUID,
    review_id: uuid.UUID,
    severity: str = "should_fix",
    display_id: int = 1,
) -> uuid.UUID:
    """Insert a canonical finding row; return its id."""
    finding_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO findings"
            " (id, org_id, pr_id, review_id, finding_display_id,"
            "  category, severity, confidence, rationale, rule_violated, rule_source, suggested_fix)"
            " VALUES (:id, :org_id, :pr_id, :review_id, :display_id,"
            "         'correctness', :severity, 'plausible', 'r', 'rule', 'src', 'fix')"
        ),
        {
            "id": finding_id,
            "org_id": org_id,
            "pr_id": pr_id,
            "review_id": review_id,
            "display_id": display_id,
            "severity": severity,
        },
    )
    return finding_id


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.service
async def test_reviewer_writes_findings_summary_on_review_end(db_session) -> None:  # type: ignore[no-untyped-def]
    """refresh_ticket_findings_summary writes count + max_severity to the ticket row."""
    org_id = uuid.uuid4()
    ticket_id, pr_id = await _seed_ticket_and_pr(db_session, org_id=org_id)
    rev_id = await _seed_review(db_session, pr_id=pr_id, org_id=org_id, seq=1)
    await _seed_finding(
        db_session, pr_id=pr_id, org_id=org_id, review_id=rev_id, severity="nit", display_id=1
    )
    await _seed_finding(
        db_session, pr_id=pr_id, org_id=org_id, review_id=rev_id, severity="blocker", display_id=2
    )
    await db_session.commit()

    await refresh_ticket_findings_summary(ticket_id, pr_id, org_id=org_id, session=db_session)
    await db_session.commit()

    row = await get_ticket(ticket_id, org_id=org_id)
    assert row.findings_count == 2
    assert row.max_severity == "blocker"


@pytest.mark.service
async def test_reviewer_writes_summary_zero_when_no_findings(db_session) -> None:  # type: ignore[no-untyped-def]
    """refresh_ticket_findings_summary with no findings sets count=0, severity=None."""
    org_id = uuid.uuid4()
    ticket_id, pr_id = await _seed_ticket_and_pr(db_session, org_id=org_id)
    # Pre-populate the rollup so we can verify it changes after refresh.
    await update_findings_summary(ticket_id, findings_count=5, max_severity="blocker", session=db_session)
    await db_session.commit()

    await refresh_ticket_findings_summary(ticket_id, pr_id, org_id=org_id, session=db_session)
    await db_session.commit()

    row = await get_ticket(ticket_id, org_id=org_id)
    assert row.findings_count == 0
    assert row.max_severity is None
