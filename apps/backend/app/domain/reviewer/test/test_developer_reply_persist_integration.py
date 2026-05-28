"""Integration test for the developer-reply ack persist chain.

Doesn't run the classifier (covered by `test_classify_reply.py`) or the VCS
post (covered by e2e). Exercises everything BETWEEN the classifier output
and the bus dispatch:

1. `apply_classified_reply` mutates the aggregate (acknowledge transition).
2. `agg_repo.save` persists the new `AcknowledgmentDecision` + the
   finding's `state` change + the appended message in FK order.
3. `dispatch_audits` writes audit rows for `FindingStateChanged` +
   `FindingAcknowledged`.
4. `dispatch_events` drains the bus.

This is the most fragile orchestration in the reply path — the
rationale-walking and the event/audit wiring are easy to drop.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.domain.reviewer.llm import ClassifyReplyOutput
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.service import (
    apply_classified_reply,
    dispatch_audits,
    dispatch_events,
)

# Cross-module persist + audit + event chain (reviewer aggregate ↔ repository ↔
# audit_log ↔ core/sse bus). Service tier.
pytestmark = pytest.mark.service


async def _seed_pr_review_and_finding(
    db_session,  # type: ignore[no-untyped-def]
    *,
    pr_id: uuid.UUID,
    review_id: uuid.UUID,
    finding_id: uuid.UUID,
    thread_id: uuid.UUID,
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
            " VALUES (:id, :org_id, :pr_id, 1, 'posted', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO findings"
            " (id, org_id, pr_id, fingerprint_hash, rule_id, title, body, rationale,"
            "  concrete_failure_scenario, confidence, severity, state, current_anchor, source_agent,"
            "  first_seen_review_id, last_observed_review_id)"
            " VALUES (:id, :org_id, :pr_id, :fp, 'r/x', 'finding title', 'finding body', 'r',"
            "         'caller invokes f() without arg; raises TypeError.', 90, 'major', 'open',"
            "         (:anchor)::jsonb, 'test', :rid, :rid)"
        ),
        {
            "id": finding_id,
            "org_id": org_id,
            "pr_id": pr_id,
            "fp": uuid.uuid4().hex,
            "anchor": (
                '{"file_path": "src/foo.py", "line_start": 1, "line_end": 1, '
                '"surrounding_content_hash": "h", "commit_sha": "abc"}'
            ),
            "rid": review_id,
        },
    )
    await db_session.execute(
        text("INSERT INTO comment_threads (id, finding_id, external_thread_id) VALUES (:id, :fid, NULL)"),
        {"id": thread_id, "fid": finding_id},
    )


@pytest.mark.asyncio
async def test_high_confidence_wontfix_reply_acks_finding_with_audit(db_session) -> None:  # type: ignore[no-untyped-def]
    """End-to-end (sans classifier + VCS post):

    apply_classified_reply(ACK_WONTFIX, 0.92) → aggregate.acknowledge() →
    save → audits → events. Finding state lands `acknowledged`; audit rows
    for `finding_state_changed` + `finding_acknowledged` exist.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await _seed_pr_review_and_finding(
        db_session,
        pr_id=pr_id,
        review_id=review_id,
        finding_id=finding_id,
        thread_id=thread_id,
        org_id=org_id,
    )
    await db_session.commit()

    repo = SqlAlchemyAggregateRepository(db_session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)

    # Append the developer's reply.
    dev_msg = aggregate.append_message(
        thread_id=thread_id,
        author_kind="human",
        author_external_id="dev",
        external_comment_id="github-1",
        in_reply_to_external_id=None,
        body="wontfix; intentional design choice — we throw early on missing config.",
    )

    # Stub classifier output as if the real classifier had run.
    classification = ClassifyReplyOutput(
        intent="acknowledgment_clear",
        suggested_ack_kind="wontfix",
        parsed_claims=None,
    )

    action = apply_classified_reply(
        aggregate,
        finding_id=finding_id,
        classification=classification,
        reply_message=dev_msg,
    )
    assert action.kind == "acknowledge_posted"
    assert action.reply_body is not None  # "Noted — I'll skip this..."

    # Persist + dispatch.
    await repo.save(aggregate)
    await dispatch_audits(aggregate, session=db_session, actor=Actor.system(), org_id=org_id)
    async with org_context(org_id, ActorKind.SYSTEM):
        events = dispatch_events(db_session, aggregate=aggregate)

    # Aggregate state is `acknowledged`.
    state = (
        await db_session.execute(text("SELECT state FROM findings WHERE id=:id"), {"id": finding_id})
    ).scalar_one()
    assert state == "acknowledged"

    # AcknowledgmentDecision landed in the DB.
    ack_row = (
        await db_session.execute(
            text("SELECT kind, rationale FROM acknowledgment_decisions WHERE finding_id=:fid"),
            {"fid": finding_id},
        )
    ).all()
    assert len(ack_row) == 1
    assert ack_row[0][0] == "wontfix"

    # Audit rows for both events.
    audit_kinds = [
        r[0]
        for r in (
            await db_session.execute(
                text("SELECT kind FROM audit_entries WHERE entity_kind='finding' AND entity_id=:fid"),
                {"fid": finding_id},
            )
        ).all()
    ]
    assert "finding_state_changed" in audit_kinds
    assert "finding_acknowledged" in audit_kinds

    # Event bus saw the same.
    event_kinds = [type(e).__name__ for e in events]
    assert "FindingStateChanged" in event_kinds
    assert "FindingAcknowledged" in event_kinds


@pytest.mark.asyncio
async def test_acknowledgment_unclear_confirm_request_no_state_change_no_ack_audit(db_session) -> None:  # type: ignore[no-untyped-def]
    """An `acknowledgment_unclear` reply gets a confirm-request,
    NOT a state change. No `finding_acknowledged` audit until the developer
    replies `confirm`.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await _seed_pr_review_and_finding(
        db_session,
        pr_id=pr_id,
        review_id=review_id,
        finding_id=finding_id,
        thread_id=thread_id,
        org_id=org_id,
    )
    await db_session.commit()

    repo = SqlAlchemyAggregateRepository(db_session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)

    dev_msg = aggregate.append_message(
        thread_id=thread_id,
        author_kind="human",
        author_external_id="dev",
        external_comment_id="github-1",
        in_reply_to_external_id=None,
        body="not sure this matters",
    )

    classification = ClassifyReplyOutput(
        intent="acknowledgment_unclear",
        suggested_ack_kind="wontfix",
        parsed_claims=None,
    )

    action = apply_classified_reply(
        aggregate,
        finding_id=finding_id,
        classification=classification,
        reply_message=dev_msg,
    )
    assert action.kind == "confirm_requested"

    await repo.save(aggregate)
    await dispatch_audits(aggregate, session=db_session, actor=Actor.system(), org_id=org_id)
    async with org_context(org_id, ActorKind.SYSTEM):
        dispatch_events(db_session, aggregate=aggregate)

    # State must still be `open` — no transition until the developer confirms.
    state = (
        await db_session.execute(text("SELECT state FROM findings WHERE id=:id"), {"id": finding_id})
    ).scalar_one()
    assert state == "open"

    audit_kinds = [
        r[0]
        for r in (
            await db_session.execute(
                text("SELECT kind FROM audit_entries WHERE entity_kind='finding' AND entity_id=:fid"),
                {"fid": finding_id},
            )
        ).all()
    ]
    assert "finding_acknowledged" not in audit_kinds, (
        f"Mid-band confirm-request must NOT emit a finding_acknowledged audit; got {audit_kinds}"
    )
