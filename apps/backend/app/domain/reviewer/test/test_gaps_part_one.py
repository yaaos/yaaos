"""Tests pinning aggregate-gate and ack-rationale behavior.

Covered:
- Off-diff finding suppression: findings on files NOT in the current diff
  get dropped by the aggregate.
- Cross-file dedup: when N findings share a rule_id via
  `duplicate_of_rule_ids`, the survivor's body carries a file list.
- Mid-band ack stores the *original* rationale (the developer's
  wontfix/intentional message), not the bare "confirm" reply.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.domain.reviewer.aggregate import PRReviewAggregate, RawFinding
from app.domain.reviewer.types import (
    CodeAnchor,
    FindingFingerprint,
    ReviewScope,
    ReviewTrigger,
)


def _raw(
    rule_id: str = "r/x",
    file_path: str = "src/foo.py",
    line: int = 1,
    duplicate_of_rule_ids: list[str] | None = None,
) -> RawFinding:
    return RawFinding(
        fingerprint=FindingFingerprint(
            file_path=file_path,
            rule_id=rule_id,
            anchor_content_hash=f"anc-{file_path}-{line}",
            body_gist_hash=f"gist-{rule_id}",
        ),
        rule_id=rule_id,
        title="t",
        body="b",
        rationale="r",
        concrete_failure_scenario="caller can pass None and dereference raises NoneType.",
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path=file_path,
            line_start=line,
            line_end=line,
            surrounding_content_hash="surr",
            commit_sha="abc",
        ),
        source_agent="test",
        duplicate_of_rule_ids=duplicate_of_rule_ids or [],
    )


# ─── Off-diff suppression ───────────────────────────────────────────────────


def test_off_diff_findings_dropped_when_diff_files_supplied() -> None:
    """Findings whose anchor file isn't in `diff_files` are dropped silently
    unless the raw finding carries an explicit causation justification.

    Suppressed unless the model explicitly justifies the off-diff anchor;
    without a justification, drop.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = PRReviewAggregate(pr_id=pr_id, org_id=org_id, now=datetime(2026, 5, 17, tzinfo=UTC))
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )

    raw = [
        _raw(rule_id="r1", file_path="src/in_diff.py", line=1),
        _raw(rule_id="r2", file_path="src/NOT_in_diff.py", line=1),
    ]
    new, _obs, drops = agg.post_process_raw_findings(review.id, raw, diff_files={"src/in_diff.py"})
    kept_files = {f.current_anchor.file_path for f in new}
    assert kept_files == {"src/in_diff.py"}
    dropped_rules = {d.rule_id for d in drops if d.reason == "off_diff"}
    assert "r2" in dropped_rules


def test_off_diff_findings_not_dropped_when_no_diff_files_supplied() -> None:
    """When the caller doesn't pass `diff_files`, off-diff suppression is
    inactive (back-compat with full-review callers that don't have a clean
    file list)."""
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = PRReviewAggregate(pr_id=pr_id, org_id=org_id, now=datetime(2026, 5, 17, tzinfo=UTC))
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    raw = [_raw(file_path="src/anywhere.py")]
    new, _, _ = agg.post_process_raw_findings(review.id, raw)
    assert len(new) == 1


# ─── Cross-file dedup with file list ───────────────────────────────────────


def test_cross_file_dedup_carries_file_list() -> None:
    """Same root issue across N files → one finding with a
    `file_list` annotation on the survivor's body listing the duplicates.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    agg = PRReviewAggregate(pr_id=pr_id, org_id=org_id, now=datetime(2026, 5, 17, tzinfo=UTC))
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    raw = [
        _raw(rule_id="security/sql-injection", file_path="src/a.py"),
        _raw(
            rule_id="security/sql-injection-b",
            file_path="src/b.py",
            duplicate_of_rule_ids=["security/sql-injection"],
        ),
        _raw(
            rule_id="security/sql-injection-c",
            file_path="src/c.py",
            duplicate_of_rule_ids=["security/sql-injection"],
        ),
    ]
    new, _, _ = agg.post_process_raw_findings(review.id, raw)
    assert len(new) == 1
    survivor = new[0]
    # Survivor's body must mention the duplicated files.
    assert "src/a.py" in survivor.body
    assert "src/b.py" in survivor.body
    assert "src/c.py" in survivor.body


# ─── Mid-band ack rationale ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mid_band_ack_uses_original_rationale(db_session) -> None:  # type: ignore[no-untyped-def]
    """When a developer types `confirm` after a yaaos mid-band confirm-request,
    the AcknowledgmentDecision.rationale must be the developer's *original*
    wontfix/intentional message — NOT the bare `confirm` reply.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.domain.reviewer.models import (  # noqa: PLC0415
        AcknowledgmentDecisionRow,
    )
    from app.domain.reviewer.replies import handle_developer_reply  # noqa: PLC0415

    org_id = uuid.uuid4()
    ticket_id, pr_id = uuid.uuid4(), uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets"
            " (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'acme/web#1', 't',"
            "         'in_review', 'github', 'acme/web')"
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
        text("UPDATE tickets SET pr_id = :pr_id WHERE id = :id"),
        {"pr_id": pr_id, "id": ticket_id},
    )
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'posted', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )

    # Seed a finding + thread + the prior developer message + yaaos
    # confirm-request reply (so handle_developer_reply detects mid-band).
    finding_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO findings (id, org_id, pr_id, fingerprint_hash, rule_id, title, body,"
            " rationale, concrete_failure_scenario, confidence, severity, state, current_anchor,"
            " source_agent, first_seen_review_id, last_observed_review_id)"
            " VALUES (:id, :org_id, :pr_id, 'hash', 'r/x', 't', 'b', 'r', 's',"
            "         90, 'major', 'open',"
            '         \'{"file_path": "src/foo.py", "line_start": 1, "line_end": 1,'
            '           "surrounding_content_hash": "", "commit_sha": "abc"}\'::jsonb,'
            "         'test', :rev, :rev)"
        ),
        {"id": finding_id, "org_id": org_id, "pr_id": pr_id, "rev": review_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO comment_threads (id, finding_id, external_thread_id)"
            " VALUES (:id, :fid, 'gh-thread-1')"
        ),
        {"id": thread_id, "fid": finding_id},
    )
    # Developer's ORIGINAL wontfix message — this is what the rationale should be.
    original_dev_msg_id = uuid.uuid4()
    original_rationale = "by design — we accept the None case at this boundary"
    await db_session.execute(
        text(
            "INSERT INTO comment_messages (id, thread_id, author_kind, author_external_id,"
            " external_comment_id, body)"
            " VALUES (:id, :tid, 'human', 'dev1', 'gh-c-1', :body)"
        ),
        {"id": original_dev_msg_id, "tid": thread_id, "body": original_rationale},
    )
    # Yaaos's mid-band confirm-request (its body contains the literal
    # "reply `confirm`" marker that `_last_message_was_confirm_request` checks).
    await db_session.execute(
        text(
            "INSERT INTO comment_messages (id, thread_id, author_kind, author_external_id,"
            " external_comment_id, in_reply_to_external_id, body)"
            " VALUES (:id, :tid, 'yaaos', 'yaaos', 'gh-c-2', 'gh-c-1',"
            " 'Reading this as ''intentional / wontfix'' — reply `confirm` to acknowledge…')"
        ),
        {"id": uuid.uuid4(), "tid": thread_id},
    )
    await db_session.commit()

    # Developer types "confirm" — handle_developer_reply must look back at
    # the original message for the rationale.
    # org_context is required because dispatch_events reads require_org_context().
    async with org_context(org_id, ActorKind.SYSTEM):
        await handle_developer_reply(
            external_thread_id="gh-thread-1",
            external_comment_id="gh-c-3",
            in_reply_to_external_id="gh-c-2",
            body="confirm",
            author_external_id="dev1",
            org_id=org_id,
        )
    await db_session.commit()

    ack = (
        await db_session.execute(
            select(AcknowledgmentDecisionRow).where(AcknowledgmentDecisionRow.finding_id == finding_id)
        )
    ).scalar_one()
    assert ack.rationale == original_rationale, (
        f"Expected the original wontfix message as the ack rationale; got: {ack.rationale!r}"
    )
