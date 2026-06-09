"""Unit tests for the reviewer service helpers.

Tests `is_yaaos_command`, `is_off_topic_message`, and `aggregate_findings_by_prs`
(severity-rank ordering).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.domain.reviewer.service import (
    aggregate_findings_by_prs,
    is_off_topic_message,
    is_yaaos_command,
)

# ─── is_yaaos_command ────────────────────────────────────────────────────────


def test_yaaos_command_review() -> None:
    assert is_yaaos_command("@yaaos review please") == "review"


def test_yaaos_command_full_review() -> None:
    assert is_yaaos_command("@yaaos full review") == "full review"


def test_yaaos_command_cancel() -> None:
    assert is_yaaos_command("@yaaos cancel") == "cancel"


def test_yaaos_command_case_insensitive() -> None:
    assert is_yaaos_command("@YAAOS REVIEW this") == "review"


def test_yaaos_command_none_when_absent() -> None:
    assert is_yaaos_command("just a regular comment") is None


# ─── is_off_topic_message ────────────────────────────────────────────────────


def test_off_topic_short_no_question_no_fix() -> None:
    assert is_off_topic_message("lgtm") is True


def test_not_off_topic_has_question() -> None:
    assert is_off_topic_message("why did you do this?") is False


def test_not_off_topic_has_fix_claim() -> None:
    assert is_off_topic_message("i fixed this issue") is False


def test_not_off_topic_long_message() -> None:
    long_msg = " ".join(["word"] * 10)
    assert is_off_topic_message(long_msg) is False


# ─── aggregate_findings_by_prs ───────────────────────────────────────────────


async def _seed_pr(db_session, pr_id: uuid.UUID, org_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', :ext, 't', 'in_review', 'github', 'acme/web')"
        ),
        {"id": ticket_id, "org_id": org_id, "ext": f"acme/web#{pr_id.hex[:8]}"},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, plugin_id, repo_external_id, external_id, ticket_id,"
            "  number, title, body, author_login, author_type, base_branch, head_branch,"
            "  base_sha, head_sha, is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, 'github', 'acme/web', :ext, :ticket_id,"
            "  1, 't', '', 'dev', 'user', 'main', 'feat', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "ext": f"acme/web#{pr_id.hex[:8]}", "ticket_id": ticket_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status)"
            " VALUES (:id, :org_id, :pr_id, 1, 'done')"
        ),
        {"id": uuid.uuid4(), "org_id": org_id, "pr_id": pr_id},
    )


async def _seed_finding(
    db_session,  # type: ignore[no-untyped-def]
    pr_id: uuid.UUID,
    org_id: uuid.UUID,
    severity: str,
    display_id: int,
    review_id: uuid.UUID,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO findings (id, org_id, pr_id, review_id, finding_display_id,"
            " category, severity, confidence, rationale, rule_violated, rule_source, suggested_fix)"
            " VALUES (:id, :org_id, :pr_id, :rev_id, :display_id,"
            " 'correctness', :severity, 'plausible', 'r', 'rule', 'src', 'fix')"
        ),
        {
            "id": uuid.uuid4(),
            "org_id": org_id,
            "pr_id": pr_id,
            "rev_id": review_id,
            "display_id": display_id,
            "severity": severity,
        },
    )


@pytest.mark.service
async def test_aggregate_findings_empty_pr_ids(db_session) -> None:  # type: ignore[no-untyped-def]
    result = await aggregate_findings_by_prs([], org_id=uuid.uuid4())
    assert result == {}


@pytest.mark.service
async def test_aggregate_findings_returns_count_and_max_severity(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid.uuid4()
    pr_id = uuid.uuid4()
    rev_id = uuid.uuid4()
    now = datetime.now(UTC)

    await _seed_pr(db_session, pr_id, org_id)
    # Update the review id to match what we'll seed findings against.
    await db_session.execute(
        text("UPDATE reviews SET id = :rev_id WHERE pr_id = :pr_id"),
        {"rev_id": rev_id, "pr_id": pr_id},
    )
    await _seed_finding(db_session, pr_id, org_id, "nit", 1, rev_id)
    await _seed_finding(db_session, pr_id, org_id, "blocker", 2, rev_id)
    await db_session.flush()

    result = await aggregate_findings_by_prs([pr_id], org_id=org_id)
    assert pr_id in result
    count, max_sev = result[pr_id]
    assert count == 2
    assert max_sev == "blocker"
    _ = now
