"""Unit tests for the durable-findings service helpers.

All tests use the in-memory aggregate (no DB) and the canned ClassifyReplyOutput
to drive `apply_classified_reply`, `apply_verify_fix_result`, and
`apply_stale_check_result` through their thresholds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.reviewer.aggregate import PRReviewAggregate, RawFinding
from app.domain.reviewer.llm import ClassifyReplyOutput
from app.domain.reviewer.service import (
    all_conversations_view,
    apply_classified_reply,
    apply_stale_check_result,
    apply_verify_fix_result,
    is_off_topic_message,
    is_yaaos_command,
    list_findings_view,
)
from app.domain.reviewer.types import (
    CodeAnchor,
    FindingFingerprint,
    FindingState,
    ReviewScope,
    ReviewTrigger,
)


def _agg() -> PRReviewAggregate:
    return PRReviewAggregate(pr_id=uuid.uuid4(), org_id=uuid.uuid4(), now=datetime(2026, 5, 17, tzinfo=UTC))


def _seed_finding(agg: PRReviewAggregate, rule: str = "r/x"):  # type: ignore[no-untyped-def]
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    fp = FindingFingerprint(
        file_path="src/foo.py",
        rule_id=rule,
        anchor_content_hash="anc",
        body_gist_hash="gist",
    )
    rf = RawFinding(
        fingerprint=fp,
        rule_id=rule,
        title="t",
        body="b",
        rationale="r",
        concrete_failure_scenario="scenario describing the failure path concretely.",
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path="src/foo.py",
            line_start=1,
            line_end=1,
            surrounding_content_hash="surr",
            commit_sha="abc",
        ),
        source_agent="agent",
    )
    new, _, _ = agg.post_process_raw_findings(review.id, [rf])
    return review, new[0]


def _msg(agg: PRReviewAggregate, thread_id: uuid.UUID, body: str):  # type: ignore[no-untyped-def]
    return agg.append_message(
        thread_id=thread_id,
        author_kind="human",
        author_external_id="dev1",
        external_comment_id=f"c-{uuid.uuid4()}",
        body=body,
    )


# ─── deterministic checks ──────────────────────────────────────────────────


def test_is_yaaos_command_matches_review() -> None:
    assert is_yaaos_command("@yaaos review please") == "review"


def test_is_yaaos_command_matches_full_review() -> None:
    assert is_yaaos_command("can you do a @yaaos full review on this?") == "full review"


def test_is_yaaos_command_returns_none_for_non_command() -> None:
    assert is_yaaos_command("thanks @yaaos") is None


def test_off_topic_short_no_question_no_fix() -> None:
    assert is_off_topic_message("thanks")
    assert is_off_topic_message("ok cool")


def test_off_topic_false_on_question() -> None:
    assert not is_off_topic_message("why?")


def test_off_topic_false_on_fix_claim() -> None:
    assert not is_off_topic_message("fixed")


def test_off_topic_false_on_long_message() -> None:
    assert not is_off_topic_message("this is a longer message that probably has substance")


# ─── apply_classified_reply ────────────────────────────────────────────────


def test_acknowledge_high_confidence_transitions_and_posts() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    msg = _msg(agg, thread.id, "by design")

    action = apply_classified_reply(
        agg,
        finding_id=finding.id,
        classification=ClassifyReplyOutput(
            intent="acknowledgment", confidence=0.92, suggested_ack_kind="intentional"
        ),
        reply_message=msg,
    )

    assert action.kind == "acknowledge_posted"
    assert action.reply_body and "skip" in action.reply_body.lower()
    assert agg.findings[0].state == FindingState.ACKNOWLEDGED


def test_acknowledge_mid_confidence_requests_confirmation() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    msg = _msg(agg, thread.id, "won't change")

    action = apply_classified_reply(
        agg,
        finding_id=finding.id,
        classification=ClassifyReplyOutput(
            intent="acknowledgment", confidence=0.70, suggested_ack_kind="wontfix"
        ),
        reply_message=msg,
    )

    assert action.kind == "confirm_requested"
    assert agg.findings[0].state == FindingState.OPEN  # no transition


def test_acknowledge_low_confidence_no_op() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    msg = _msg(agg, thread.id, "hm")

    action = apply_classified_reply(
        agg,
        finding_id=finding.id,
        classification=ClassifyReplyOutput(intent="acknowledgment", confidence=0.4),
        reply_message=msg,
    )

    assert action.kind == "noop"
    assert agg.findings[0].state == FindingState.OPEN


def test_verify_fix_high_confidence_triggers_subflow() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    msg = _msg(agg, thread.id, "fixed in abc")

    action = apply_classified_reply(
        agg,
        finding_id=finding.id,
        classification=ClassifyReplyOutput(intent="verify_fix", confidence=0.95),
        reply_message=msg,
    )

    assert action.kind == "verify_fix_triggered"


def test_other_intent_no_op() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    msg = _msg(agg, thread.id, "how about renaming this?")

    action = apply_classified_reply(
        agg,
        finding_id=finding.id,
        classification=ClassifyReplyOutput(intent="other", confidence=0.95),
        reply_message=msg,
    )

    assert action.kind == "noop"


# ─── apply_verify_fix_result ────────────────────────────────────────────────


def test_verify_fix_resolves_when_not_present_and_confident() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)

    action = apply_verify_fix_result(agg, finding_id=finding.id, still_present=False, confidence=0.95)

    assert action.kind == "resolved"
    assert agg.findings[0].state == FindingState.RESOLVED_CONFIRMED


def test_verify_fix_observes_still_present_at_high_confidence() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)

    action = apply_verify_fix_result(
        agg, finding_id=finding.id, still_present=True, confidence=0.95, observed_line=14
    )

    assert action.kind == "still_present_observed"
    assert "line 14" in action.reply_body
    assert agg.findings[0].state == FindingState.OPEN


def test_verify_fix_observes_at_mid_confidence_without_transitioning() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)

    action = apply_verify_fix_result(agg, finding_id=finding.id, still_present=False, confidence=0.65)

    assert action.kind == "low_confidence_noop"
    assert "Unclear" in action.reply_body
    assert agg.findings[0].state == FindingState.OPEN


def test_verify_fix_low_confidence_silent() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)

    action = apply_verify_fix_result(agg, finding_id=finding.id, still_present=False, confidence=0.3)

    assert action.kind == "low_confidence_noop"
    assert action.reply_body == ""
    assert agg.findings[0].state == FindingState.OPEN


# ─── apply_stale_check_result ───────────────────────────────────────────────


def test_stale_check_marks_stale_when_high_confidence() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)

    action = apply_stale_check_result(agg, finding_id=finding.id, still_applies=False, confidence=0.95)

    assert action.kind == "stale_marked"
    assert agg.findings[0].state == FindingState.STALE


def test_stale_check_observes_when_still_applies() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)

    action = apply_stale_check_result(agg, finding_id=finding.id, still_applies=True, confidence=0.95)

    assert action.kind == "still_applies_observed"
    assert agg.findings[0].state == FindingState.OPEN


# ─── list_findings_view + all_conversations_view ────────────────────────────


def test_list_findings_view_default_excludes_resolved() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    agg.mark_unverified_resolution(finding.id)

    view = list_findings_view(agg)

    assert view == []


def test_list_findings_view_with_include_terminal_returns_resolved() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    agg.mark_unverified_resolution(finding.id)

    view = list_findings_view(agg, include_terminal=True)

    assert len(view) == 1
    assert view[0].state == FindingState.RESOLVED_UNVERIFIED


def test_all_conversations_view_includes_threads_with_replies() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    _msg(agg, thread.id, "I have a question about this")

    view = all_conversations_view(agg)

    assert len(view) == 1
    assert view[0].finding_id == finding.id
    assert view[0].reply_count == 1


def test_all_conversations_view_excludes_stale() -> None:
    agg = _agg()
    _, finding = _seed_finding(agg)
    thread = agg.open_thread_for_finding(finding.id)
    _msg(agg, thread.id, "x")
    agg.record_stale_detection(finding_id=finding.id, still_applies=False, confidence=0.95)

    view = all_conversations_view(agg)

    assert view == []
