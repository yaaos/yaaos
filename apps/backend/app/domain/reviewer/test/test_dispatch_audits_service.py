"""Service test: dispatch_audits writes FindingAuditPayload-shaped rows.

Asserts that the `audit_entries.payload` column deserializes back into a
`FindingAuditPayload` — confirming the payload is typed and structured, not
an opaque envelope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.audit_log import Actor, ActorKind
from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.service import FindingAuditPayload, dispatch_audits
from app.domain.reviewer.types import (
    CodeAnchor,
    CommentMessage,
    CommentThread,
    Finding,
    FindingFingerprint,
    FindingState,
)


def _seeded_aggregate(*, finding_id: uuid.UUID) -> PRReviewAggregate:
    """Aggregate with one OPEN finding, one thread, and one developer message."""
    pr_id = uuid.uuid4()
    org_id = uuid.uuid4()
    review_id = uuid.uuid4()
    anchor = CodeAnchor(
        file_path="src/foo.py",
        line_start=1,
        line_end=1,
        surrounding_content_hash="hash",
        commit_sha="abc",
    )
    finding = Finding(
        id=finding_id,
        pr_id=pr_id,
        org_id=org_id,
        fingerprint=FindingFingerprint(
            file_path="src/foo.py",
            rule_id="r/x",
            anchor_content_hash="hash",
            body_gist_hash="gist",
        ),
        rule_id="r/x",
        title="t",
        body="b",
        rationale="r",
        concrete_failure_scenario="caller invokes f() without arg; raises TypeError.",
        confidence=90,
        severity="major",
        state=FindingState.OPEN,
        current_anchor=anchor,
        source_agent="test",
        first_seen_review_id=review_id,
        last_observed_review_id=review_id,
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
        updated_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    thread = CommentThread(
        id=uuid.uuid4(),
        finding_id=finding_id,
        external_thread_id=None,
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
        updated_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    dev_msg = CommentMessage(
        id=uuid.uuid4(),
        thread_id=thread.id,
        author_kind="human",
        author_external_id="dev",
        external_comment_id="github-1",
        in_reply_to_external_id=None,
        body="wontfix; intentional",
        classified_intent=None,
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    return PRReviewAggregate(
        pr_id=pr_id,
        org_id=org_id,
        findings=[finding],
        threads=[thread],
        messages=[dev_msg],
        now=datetime(2026, 5, 17, tzinfo=UTC),
    )


@pytest.mark.service
@pytest.mark.asyncio
async def test_dispatch_audits_writes_pydantic_payload(db_session) -> None:  # type: ignore[no-untyped-def]
    """Audit row payload deserializes to FindingAuditPayload with expected fields."""
    finding_id = uuid.uuid4()
    agg = _seeded_aggregate(finding_id=finding_id)

    dev_msg = agg.messages[0]
    agg.acknowledge(
        finding_id=finding_id,
        kind="wontfix",
        rationale="dev said wontfix",
        made_by_external_id=dev_msg.author_external_id,
        made_by_message_id=dev_msg.id,
    )

    actor = Actor(kind=ActorKind.SYSTEM)
    await dispatch_audits(agg, session=db_session, actor=actor, org_id=agg.org_id)

    rows = (
        await db_session.execute(
            text("SELECT kind, payload FROM audit_entries WHERE entity_kind='finding' AND entity_id=:fid"),
            {"fid": finding_id},
        )
    ).all()

    assert rows, "Expected at least one audit row for the finding"

    ack_rows = [(kind, payload) for kind, payload in rows if kind == "finding_acknowledged"]
    assert ack_rows, f"Expected finding_acknowledged audit row; got kinds={[r[0] for r in rows]}"

    _kind_str, payload_dict = ack_rows[0]

    # Payload must round-trip into FindingAuditPayload without error.
    audit_payload = FindingAuditPayload(
        kind=payload_dict["kind"],
        finding_id=payload_dict["finding_id"],
        fields=payload_dict["fields"],
    )
    assert audit_payload.kind == "finding_acknowledged"
    assert audit_payload.finding_id == finding_id
    assert "finding_id" in audit_payload.fields
