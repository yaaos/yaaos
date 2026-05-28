"""Integration test for the durable-findings persist + audit + dispatch chain.

Doesn't spin up the workspace + agent + VCS plugins. Exercises the inner
persist chain:

1. Agent emits a `FindingDraft`.
2. `_findingdrafts_to_raw` converts to `RawFinding` using REAL file content
   (so anchor + fingerprint hashes are stable).
3. `aggregate.post_process_raw_findings` admits the finding against the
   diff_files allowlist and severity thresholds.
4. `agg_repo.save` flushes findings → observations/threads → messages →
   acks in FK order.
5. `dispatch_audits` writes an `audit_entries` row per state-changing
   event.
6. `dispatch_events` (run last — drains the events list) publishes
   to the SSE bus via `core/sse`.

Initial review and push-incremental review both use the same helpers;
this test catches wiring regressions in either path.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.domain.coding_agent import FindingAnchor, FindingDraft
from app.domain.reviewer.admission import (
    findingdrafts_to_raw as _findingdrafts_to_raw,
)
from app.domain.reviewer.admission import (
    raw_to_vcs_findings as _raw_to_vcs_findings,
)
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.service import dispatch_audits, dispatch_events

# Drives the durable-findings persist + admission + audit + event chain
# across reviewer aggregate ↔ repository ↔ audit_log ↔ events. Service tier.
pytestmark = pytest.mark.service


async def _seed_pr_and_review(
    db_session,  # type: ignore[no-untyped-def]
    *,
    pr_id: uuid.UUID,
    review_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
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
    await db_session.execute(
        text(
            "INSERT INTO reviews"
            " (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'running', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )


def _draft() -> FindingDraft:
    return FindingDraft(
        severity="major",
        rule_id="security/sql-injection",
        title="Unparameterized query interpolates user input",
        body=(
            "`username` flows straight into the f-string without parameterization. Use a parameterized query."
        ),
        concrete_failure_scenario=("Attacker submits `'; DROP TABLE users; --` as username; query executes."),
        confidence=92,
        rationale="OWASP A03:2021 — SQL injection.",
        anchor=FindingAnchor(file_path="src/foo.py", line_start=10, line_end=10),
    )


@pytest.mark.asyncio
async def test_finding_persists_with_audit_and_anchor_lines(db_session) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: draft → raw → admit → save → audits + events."""
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = uuid.uuid4()
    await _seed_pr_and_review(db_session, pr_id=pr_id, review_id=review_id, org_id=org_id)
    await db_session.commit()

    # Real file content for the anchor — what the workspace would have read.
    file_lines = [f"line {i}" for i in range(20)]
    file_lines[9] = "query = f'SELECT * FROM users WHERE name = {username}'"

    raw = _findingdrafts_to_raw(
        [_draft()],
        commit_sha="abc",
        read_file=lambda p: file_lines if p == "src/foo.py" else None,
    )
    assert len(raw) == 1
    # Anchor's `original_lines` snapshots the flagged code so verify_fix has
    # the original to compare against later.
    assert raw[0].anchor.original_lines == ("query = f'SELECT * FROM users WHERE name = {username}'",)

    repo = SqlAlchemyAggregateRepository(db_session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)

    new_findings, _obs, drops = aggregate.post_process_raw_findings(review_id, raw, diff_files={"src/foo.py"})
    assert len(new_findings) == 1
    assert drops == []

    # Translate admitted RawFinding → vcs.Finding for posting (simulated here).
    posted = _raw_to_vcs_findings(raw, new_findings)
    assert len(posted) == 1
    # severity → vcs vocab: major → must-fix.
    assert posted[0].severity == "must-fix"

    # Save persists findings + their anchors (with original_lines in JSONB).
    await repo.save(aggregate)
    await dispatch_audits(aggregate, session=db_session, actor=Actor.system(), org_id=org_id)
    async with org_context(org_id, ActorKind.SYSTEM):
        events = dispatch_events(db_session, aggregate=aggregate)

    # The aggregate emitted FindingRaised — dispatch_events drained it.
    kinds = [type(e).__name__ for e in events]
    assert "FindingRaised" in kinds

    # Audit row landed for the FindingRaised transition.
    rows = (
        await db_session.execute(
            text("SELECT kind FROM audit_entries WHERE entity_kind='finding' AND entity_id=:fid"),
            {"fid": new_findings[0].id},
        )
    ).all()
    audit_kinds = [r[0] for r in rows]
    assert "finding_raised" in audit_kinds, f"Expected finding_raised audit; got {audit_kinds}"

    # current_anchor JSONB carries `original_lines` (verify_fix needs this).
    anchor_row = (
        await db_session.execute(
            text("SELECT current_anchor FROM findings WHERE id=:id"),
            {"id": new_findings[0].id},
        )
    ).scalar_one()
    assert anchor_row["original_lines"] == ["query = f'SELECT * FROM users WHERE name = {username}'"]


@pytest.mark.asyncio
async def test_off_diff_finding_is_dropped_end_to_end(db_session) -> None:  # type: ignore[no-untyped-def]
    """A finding whose anchor file isn't in the diff is rejected
    BEFORE posting + persisting. Verifies the diff_files= wiring engaged.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = uuid.uuid4()
    await _seed_pr_and_review(db_session, pr_id=pr_id, review_id=review_id, org_id=org_id)
    await db_session.commit()

    file_lines = [f"line {i}" for i in range(20)]
    file_lines[9] = "interesting content"

    raw = _findingdrafts_to_raw(
        [_draft()],
        commit_sha="abc",
        read_file=lambda p: file_lines,
    )

    repo = SqlAlchemyAggregateRepository(db_session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)

    # Diff touches a DIFFERENT file — the finding's `src/foo.py` is off-diff.
    new_findings, _obs, drops = aggregate.post_process_raw_findings(
        review_id, raw, diff_files={"src/other.py"}
    )

    assert new_findings == []
    assert len(drops) == 1
    assert drops[0].reason == "off_diff"
