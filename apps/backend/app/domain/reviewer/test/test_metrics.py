"""Eval metrics: acceptance_rate + resolved_without_edit_rate.

`compute_acceptance_rate` counts findings whose state indicates the developer
touched the flagged code — `resolved_confirmed` (agent verified) +
`resolved_unverified` (anchor gone). `acknowledged` (wontfix) and `stale`
do NOT count as acceptance — those are explicit non-changes.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.domain.reviewer.service import (
    compute_acceptance_rate,
    compute_resolved_without_edit_rate,
)


async def _seed_pr(db_session, pr_id: uuid.UUID, org_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'acme/web#1', 't', 'in_review', 'github', 'acme/web')"
        ),
        {"id": ticket_id, "org_id": org_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id, number, title, body,"
            "  author_login, author_type, base_branch, head_branch, base_sha, head_sha,"
            "  is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', 'acme/web#1', 'acme/web', 1, 't', '',"
            "         'dev', 'user', 'main', 'feature', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "tid": ticket_id},
    )


async def _seed_finding(db_session, *, finding_id, pr_id, org_id, state: str, seq: int) -> None:  # type: ignore[no-untyped-def]
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, :seq, 'posted', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id, "seq": seq},
    )
    fingerprint = uuid.uuid4().hex  # uniqueness per row
    anchor_json = (
        '{"file_path": "src/foo.py", "line_start": 1, "line_end": 1, '
        '"surrounding_content_hash": "h", "commit_sha": "abc"}'
    )
    await db_session.execute(
        text(
            "INSERT INTO findings"
            " (id, org_id, pr_id, fingerprint_hash, rule_id, title, body, rationale,"
            "  concrete_failure_scenario, confidence, severity, state, current_anchor, source_agent,"
            "  first_seen_review_id, last_observed_review_id)"
            " VALUES (:id, :org_id, :pr_id, :fp, 'r/x', 't', 'b', 'r',"
            "         'caller invokes f() without arg; raises TypeError.', 90, 'major', :state,"
            "         (:anchor)::jsonb, 'test', :rid, :rid)"
        ),
        {
            "id": finding_id,
            "org_id": org_id,
            "pr_id": pr_id,
            "fp": fingerprint,
            "state": state,
            "anchor": anchor_json,
            "rid": review_id,
        },
    )


@pytest.mark.asyncio
async def test_acceptance_rate_counts_resolved_confirmed_and_resolved_unverified(db_session) -> None:  # type: ignore[no-untyped-def]
    """5 findings: 2 resolved_confirmed + 1 resolved_unverified + 1 acknowledged + 1 open.
    Acceptance = (2 + 1) / 5 = 0.6. `acknowledged` does NOT count.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    states = ["resolved_confirmed", "resolved_confirmed", "resolved_unverified", "acknowledged", "open"]
    for i, state in enumerate(states, start=1):
        await _seed_finding(
            db_session, finding_id=uuid.uuid4(), pr_id=pr_id, org_id=org_id, state=state, seq=i
        )
    await db_session.commit()

    rate = await compute_acceptance_rate(org_id=org_id)
    assert rate == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_acknowledged_does_not_count_as_acceptance(db_session) -> None:  # type: ignore[no-untyped-def]
    """Wontfix acks must not count toward the `resolved_confirmed` proxy.
    The proxy excludes them.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    for i in range(3):
        await _seed_finding(
            db_session,
            finding_id=uuid.uuid4(),
            pr_id=pr_id,
            org_id=org_id,
            state="acknowledged",
            seq=i + 1,
        )
    await db_session.commit()

    rate = await compute_acceptance_rate(org_id=org_id)
    assert rate == 0.0


@pytest.mark.asyncio
async def test_resolved_without_edit_rate_counts_ack_stale_unverified(db_session) -> None:  # type: ignore[no-untyped-def]
    """Resolved-without-edit captures acknowledged + stale + resolved_unverified."""
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    states = ["acknowledged", "stale", "resolved_unverified", "resolved_confirmed", "open"]
    for i, state in enumerate(states, start=1):
        await _seed_finding(
            db_session, finding_id=uuid.uuid4(), pr_id=pr_id, org_id=org_id, state=state, seq=i
        )
    await db_session.commit()

    rate = await compute_resolved_without_edit_rate(org_id=org_id)
    assert rate == pytest.approx(0.6)
