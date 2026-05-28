"""`PRReviewAggregate` unit tests covering the review flows.

Exercise the public methods via the in-memory repository to also catch
load/save asymmetries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.reviewer.aggregate import PRReviewAggregate, RawFinding
from app.domain.reviewer.events import (
    FindingAcknowledged,
    FindingRaised,
    FindingReObserved,
    FindingStateChanged,
    ReviewCompleted,
    ReviewRequested,
)
from app.domain.reviewer.test.in_memory_repository import InMemoryAggregateRepository
from app.domain.reviewer.types import (
    CodeAnchor,
    FindingFingerprint,
    FindingState,
    ReviewScope,
    ReviewTrigger,
    Severity,
)


def _raw(
    *,
    rule_id: str = "correctness/null-deref",
    title: str = "x could be None",
    severity: Severity = "major",
    confidence: int = 90,
    file_path: str = "src/foo.py",
    line: int = 10,
    anchor_hash: str | None = None,
    body_hash: str | None = None,
    scenario: str = "Caller can pass None; foo() dereferences without a check; raises NoneType error.",
) -> RawFinding:
    fp = FindingFingerprint(
        file_path=file_path,
        rule_id=rule_id,
        anchor_content_hash=anchor_hash or f"anc-{rule_id}-{line}",
        body_gist_hash=body_hash or f"gist-{rule_id}-{title}",
    )
    return RawFinding(
        fingerprint=fp,
        rule_id=rule_id,
        title=title,
        body="...",
        rationale="...",
        concrete_failure_scenario=scenario,
        confidence=confidence,
        severity=severity,
        anchor=CodeAnchor(
            file_path=file_path,
            line_start=line,
            line_end=line,
            surrounding_content_hash=f"surr-{file_path}-{line}",
            commit_sha="abc123",
        ),
        source_agent="coding_agent:full_review:v1",
    )


def _agg(pr_id: uuid.UUID, org_id: uuid.UUID) -> PRReviewAggregate:
    return PRReviewAggregate(pr_id=pr_id, org_id=org_id, now=datetime(2026, 5, 17, tzinfo=UTC))


async def test_start_review_assigns_sequence_numbers() -> None:
    pr_id = uuid.uuid4()
    org_id = uuid.uuid4()
    repo = InMemoryAggregateRepository()
    agg = await repo.load(pr_id=pr_id, org_id=org_id)

    r1 = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    r2 = agg.start_review(
        trigger=ReviewTrigger.PUSH_INCREMENTAL,
        scope=ReviewScope.incremental(prev_sha="h", head_sha="h2"),
        commit_sha="h2",
    )

    assert r1.sequence_number == 1
    assert r2.sequence_number == 2
    assert any(isinstance(e, ReviewRequested) for e in agg.events)


async def test_post_process_admits_above_threshold() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    repo = InMemoryAggregateRepository()
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    new, _obs, drops = agg.post_process_raw_findings(review.id, [_raw(severity="major", confidence=90)])

    assert len(new) == 1
    assert drops == []
    assert any(isinstance(e, FindingRaised) for e in agg.events)


async def test_post_process_drops_below_severity_threshold() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    repo = InMemoryAggregateRepository()
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    # nit needs >= 90; 80 should drop.
    new, _, drops = agg.post_process_raw_findings(review.id, [_raw(severity="nit", confidence=80)])

    assert new == []
    assert len(drops) == 1
    assert drops[0].reason == "below_threshold"


async def test_post_process_drops_missing_scenario() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    new, _, drops = agg.post_process_raw_findings(review.id, [_raw(scenario="  ")])

    assert new == []
    assert drops[0].reason == "malformed"


async def test_post_process_drops_trivially_short_scenario() -> None:
    """A finding whose `concrete_failure_scenario` is too short to describe
    an actual failure mode is treated as malformed — closes the synthesis
    loophole where a one-word body would otherwise pass the malformed gate
    with confidence=90.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    # 19 chars — below the minimum.
    new, _, drops = agg.post_process_raw_findings(review.id, [_raw(scenario="too short to use.")])

    assert new == []
    assert drops[0].reason == "malformed"


async def test_per_pr_nit_cap_enforced() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    # 7 nits, only 5 should survive (per-PR cap).
    raw = [_raw(severity="nit", confidence=95, line=i, rule_id=f"r{i}") for i in range(7)]
    new, _, drops = agg.post_process_raw_findings(review.id, raw)

    assert len(new) == 5
    assert sum(1 for d in drops if d.reason == "nit_cap") == 2


async def test_per_review_top_cap_ranks_by_severity_times_confidence() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    # 12 majors, all confidence 90 → top 10 win, ranked stable by input order.
    raw = [_raw(severity="major", confidence=90, line=i, rule_id=f"r{i}") for i in range(12)]
    new, _, drops = agg.post_process_raw_findings(review.id, raw)

    assert len(new) == 10
    assert sum(1 for d in drops if d.reason == "top_cap") == 2


async def test_re_observation_doesnt_create_new_finding() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    repo = InMemoryAggregateRepository()
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    review1 = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    raw1 = _raw(confidence=80, line=10)
    agg.post_process_raw_findings(review1.id, [raw1])
    await repo.save(agg)

    # Second review with the same fingerprint at higher confidence.
    agg2 = await repo.load(pr_id=pr_id, org_id=org_id)
    review2 = agg2.start_review(
        trigger=ReviewTrigger.PUSH_INCREMENTAL,
        scope=ReviewScope.incremental(prev_sha="h", head_sha="h2"),
        commit_sha="h2",
    )
    raw2 = _raw(confidence=95, line=10)
    new, _, _ = agg2.post_process_raw_findings(review2.id, [raw2])

    assert new == []  # no new finding raised
    assert any(isinstance(e, FindingReObserved) for e in agg2.events)
    # Confidence was bumped to max.
    persisted = agg2.findings[0]
    assert persisted.confidence == 95


async def test_acknowledged_finding_silently_dropped_on_re_observation() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    repo = InMemoryAggregateRepository()
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    review1 = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    raw = _raw()
    new, _, _ = agg.post_process_raw_findings(review1.id, [raw])
    finding = new[0]
    thread = agg.open_thread_for_finding(finding.id)
    msg = agg.append_message(
        thread_id=thread.id,
        author_kind="human",
        author_external_id="dev1",
        external_comment_id="c1",
        body="intentional",
    )
    agg.acknowledge(
        finding_id=finding.id,
        kind="intentional",
        rationale="by design",
        made_by_external_id="dev1",
        made_by_message_id=msg.id,
    )
    await repo.save(agg)

    # Second review with the same fingerprint — should drop silently.
    agg2 = await repo.load(pr_id=pr_id, org_id=org_id)
    review2 = agg2.start_review(
        trigger=ReviewTrigger.PUSH_INCREMENTAL,
        scope=ReviewScope.incremental(prev_sha="h", head_sha="h2"),
        commit_sha="h2",
    )
    new2, _, drops = agg2.post_process_raw_findings(review2.id, [raw])

    assert new2 == []
    assert drops[0].reason == "matches_ack"


async def test_acknowledge_transitions_state_and_records_decision() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    finding = new[0]
    thread = agg.open_thread_for_finding(finding.id)
    msg = agg.append_message(
        thread_id=thread.id,
        author_kind="human",
        author_external_id="dev1",
        external_comment_id="c1",
        body="we'll skip this",
    )

    ack = agg.acknowledge(
        finding_id=finding.id,
        kind="wontfix",
        rationale="too risky to change here",
        made_by_external_id="dev1",
        made_by_message_id=msg.id,
    )

    assert agg.findings[0].state == FindingState.ACKNOWLEDGED
    assert ack.kind == "wontfix"
    assert any(
        isinstance(e, FindingStateChanged) and e.to_state == FindingState.ACKNOWLEDGED for e in agg.events
    )
    assert any(isinstance(e, FindingAcknowledged) for e in agg.events)


async def test_verify_fix_confirms_when_high_confidence_and_not_present() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    finding = new[0]

    out = agg.record_fix_verification(finding_id=finding.id, still_present=False, confidence=0.95)

    assert out == FindingState.RESOLVED_CONFIRMED


async def test_verify_fix_no_op_below_threshold() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    finding = new[0]

    out = agg.record_fix_verification(finding_id=finding.id, still_present=False, confidence=0.5)

    assert out is None
    assert agg.findings[0].state == FindingState.OPEN


async def test_stale_check_transitions_when_no_longer_applies() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    finding = new[0]

    out = agg.record_stale_detection(finding_id=finding.id, still_applies=False, confidence=0.95)

    assert out == FindingState.STALE


async def test_mark_unverified_resolution() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    finding = new[0]

    agg.mark_unverified_resolution(finding.id)

    assert agg.findings[0].state == FindingState.RESOLVED_UNVERIFIED


async def test_open_findings_in_files_filters_correctly() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    agg.post_process_raw_findings(
        review.id,
        [
            _raw(file_path="src/a.py", line=1, rule_id="r1"),
            _raw(file_path="src/b.py", line=2, rule_id="r2"),
            _raw(file_path="src/c.py", line=3, rule_id="r3"),
        ],
    )

    in_a_b = agg.open_findings_in_files({"src/a.py", "src/b.py"})

    assert {f.current_anchor.file_path for f in in_a_b} == {"src/a.py", "src/b.py"}


async def test_complete_review_emits_event() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])

    agg.complete_review(review.id, [f.id for f in new])

    assert agg.reviews[0].status == "done"
    assert any(isinstance(e, ReviewCompleted) for e in agg.events)


async def test_repository_round_trip_preserves_state() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    repo = InMemoryAggregateRepository()
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    agg.complete_review(review.id, [f.id for f in new])
    await repo.save(agg)

    agg2 = await repo.load(pr_id=pr_id, org_id=org_id)

    assert len(agg2.reviews) == 1
    assert agg2.reviews[0].status == "done"
    assert len(agg2.findings) == 1
    assert agg2.findings[0].state == FindingState.OPEN


async def test_terminal_state_rejects_acknowledge() -> None:
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = _agg(pr_id, org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [_raw()])
    finding = new[0]
    agg.mark_unverified_resolution(finding.id)
    thread = agg.open_thread_for_finding(finding.id)
    msg = agg.append_message(
        thread_id=thread.id,
        author_kind="human",
        author_external_id="dev",
        external_comment_id="c",
        body="x",
    )

    with pytest.raises(ValueError, match="cannot move"):
        agg.acknowledge(
            finding_id=finding.id,
            kind="intentional",
            rationale="x",
            made_by_external_id="dev",
            made_by_message_id=msg.id,
        )
