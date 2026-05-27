"""Service tests for find_pr_id_by_external_comment_id + aggregate_findings_by_prs.

Real Postgres via `db_session`; no mocks.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.domain.reviewer.service import (
    aggregate_findings_by_prs,
    find_pr_id_by_external_comment_id,
)

# ── helpers ──────────────────────────────────────────────────────────────────

_ANCHOR_JSON = (
    '{"file_path": "src/foo.py", "line_start": 1, "line_end": 1, '
    '"surrounding_content_hash": "h", "commit_sha": "abc"}'
)


async def _seed_pr(db_session, pr_id: uuid.UUID, org_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    ticket_id = uuid.uuid4()
    # Use a unique source_external_id per seeded ticket to avoid the
    # uq_tickets_org_source_external constraint when multiple PRs share org_id.
    src_ext_id = f"acme/web#{uuid.uuid4().hex[:8]}"
    pr_ext_id = f"acme/web#{uuid.uuid4().hex[:8]}"
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', :src_ext_id, 't', 'in_review', 'github', 'acme/web')"
        ),
        {"id": ticket_id, "org_id": org_id, "src_ext_id": src_ext_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id, number, title, body,"
            "  author_login, author_type, base_branch, head_branch, base_sha, head_sha,"
            "  is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', :pr_ext_id, 'acme/web', 1, 't', '',"
            "         'dev', 'user', 'main', 'feature', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "tid": ticket_id, "pr_ext_id": pr_ext_id},
    )


async def _seed_finding(  # type: ignore[no-untyped-def]
    db_session,
    *,
    finding_id: uuid.UUID,
    pr_id: uuid.UUID,
    org_id: uuid.UUID,
    severity: str = "high",
    seq: int = 1,
) -> uuid.UUID:
    """Seed a finding row; return the review_id used."""
    review_id = uuid.uuid4()
    fp = uuid.uuid4().hex
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, :seq, 'posted', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id, "seq": seq},
    )
    await db_session.execute(
        text(
            "INSERT INTO findings"
            " (id, org_id, pr_id, fingerprint_hash, rule_id, title, body, rationale,"
            "  concrete_failure_scenario, confidence, severity, state, current_anchor, source_agent,"
            "  first_seen_review_id, last_observed_review_id)"
            " VALUES (:id, :org_id, :pr_id, :fp, 'r/x', 't', 'b', 'r',"
            "         'caller invokes f() without arg; raises TypeError.', 90, :severity, 'open',"
            "         (:anchor)::jsonb, 'test', :rid, :rid)"
        ),
        {
            "id": finding_id,
            "org_id": org_id,
            "pr_id": pr_id,
            "fp": fp,
            "severity": severity,
            "anchor": _ANCHOR_JSON,
            "rid": review_id,
        },
    )
    return review_id


async def _seed_thread_and_message(  # type: ignore[no-untyped-def]
    db_session,
    *,
    finding_id: uuid.UUID,
    external_comment_id: str,
) -> uuid.UUID:
    """Seed a comment_thread + one yaaos message; return the thread_id."""
    thread_id = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO comment_threads (id, finding_id) VALUES (:id, :finding_id)"),
        {"id": thread_id, "finding_id": finding_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO comment_messages"
            " (id, thread_id, author_kind, author_external_id, external_comment_id, body)"
            " VALUES (:id, :tid, 'yaaos', 'yaaos[bot]', :ext_id, 'body')"
        ),
        {"id": uuid.uuid4(), "tid": thread_id, "ext_id": external_comment_id},
    )
    return thread_id


# ── find_pr_id_by_external_comment_id ────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_pr_id_returns_pr_id_for_known_comment(db_session) -> None:  # type: ignore[no-untyped-def]
    """Happy path: given an external_comment_id that exists, returns the pr_id."""
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    finding_id = uuid.uuid4()
    await _seed_finding(db_session, finding_id=finding_id, pr_id=pr_id, org_id=org_id)
    ext_id = f"ext-comment-{uuid.uuid4()}"
    await _seed_thread_and_message(db_session, finding_id=finding_id, external_comment_id=ext_id)
    await db_session.commit()

    result = await find_pr_id_by_external_comment_id(ext_id)

    assert result == pr_id


@pytest.mark.asyncio
async def test_find_pr_id_returns_none_for_unknown_comment(db_session) -> None:  # type: ignore[no-untyped-def]
    """No matching row → None."""
    result = await find_pr_id_by_external_comment_id("does-not-exist-9999")

    assert result is None


# ── aggregate_findings_by_prs ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregate_findings_groups_by_pr(db_session) -> None:  # type: ignore[no-untyped-def]
    """Two PRs with different finding counts and severities are grouped correctly."""
    org_id = uuid.uuid4()
    pr1, pr2 = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr1, org_id)
    await _seed_pr(db_session, pr2, org_id)

    # pr1: 2 high + 1 medium
    await _seed_finding(db_session, finding_id=uuid.uuid4(), pr_id=pr1, org_id=org_id, severity="high", seq=1)
    await _seed_finding(db_session, finding_id=uuid.uuid4(), pr_id=pr1, org_id=org_id, severity="high", seq=2)
    await _seed_finding(
        db_session, finding_id=uuid.uuid4(), pr_id=pr1, org_id=org_id, severity="medium", seq=3
    )

    # pr2: 1 low
    await _seed_finding(db_session, finding_id=uuid.uuid4(), pr_id=pr2, org_id=org_id, severity="low", seq=1)

    await db_session.commit()

    result = await aggregate_findings_by_prs([pr1, pr2], org_id=org_id)

    assert pr1 in result
    count1, sev1 = result[pr1]
    assert count1 == 3
    assert sev1 == "high"

    assert pr2 in result
    count2, sev2 = result[pr2]
    assert count2 == 1
    assert sev2 == "low"


@pytest.mark.asyncio
async def test_aggregate_findings_empty_input_returns_empty(db_session) -> None:  # type: ignore[no-untyped-def]
    """Empty pr_ids list → empty dict without querying the DB."""
    result = await aggregate_findings_by_prs([], org_id=uuid.uuid4())

    assert result == {}


@pytest.mark.asyncio
async def test_aggregate_findings_pr_with_no_findings_absent_from_result(db_session) -> None:  # type: ignore[no-untyped-def]
    """A pr_id with no findings is not present in the result dict."""
    org_id = uuid.uuid4()
    pr_empty = uuid.uuid4()
    await _seed_pr(db_session, pr_empty, org_id)
    await db_session.commit()

    result = await aggregate_findings_by_prs([pr_empty], org_id=org_id)

    assert pr_empty not in result
