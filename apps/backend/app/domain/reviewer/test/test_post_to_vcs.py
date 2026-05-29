"""`post_admitted_findings_to_vcs` — posts admitted findings to the VCS
plugin AND attaches yaaos CommentMessage rows to each finding's thread.

Uses `StubVCSPlugin` for the GitHub side. Asserts:
- the stub recorded a post_review call with the right PR external id
- the aggregate gained one CommentMessage per admitted finding with the
  stub-returned external_comment_id
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.domain.pull_requests import upsert as upsert_pr
from app.domain.reviewer.admission import (
    admit_raw_findings,
    post_admitted_findings_to_vcs,
)
from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.models import CommentMessageRow, FindingRow
from app.domain.reviewer.types import CodeAnchor, FindingFingerprint
from app.domain.tickets import create as create_ticket
from app.domain.vcs import VCSPullRequest
from app.testing.stub_vcs import register_stub_vcs


def _real_raw_finding() -> RawFinding:
    """A RawFinding that passes the admission gates."""
    fp = FindingFingerprint(
        file_path="src/foo.py",
        rule_id="r1",
        anchor_content_hash="anc-r1-10",
        body_gist_hash="gist-r1-x",
    )
    return RawFinding(
        fingerprint=fp,
        rule_id="r1",
        title="Spy finding",
        body="Spy body",
        rationale="Spy rationale",
        concrete_failure_scenario=(
            "Caller can pass None; foo() dereferences without a check; raises NoneType."
        ),
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path="src/foo.py",
            line_start=10,
            line_end=10,
            surrounding_content_hash="surr-foo-10",
            commit_sha="deadbeef",
        ),
        source_agent="test",
    )


async def test_post_admitted_findings_to_vcs_happy_path(db_session) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: admit one finding → post via stub vcs → assert the
    finding's thread received a yaaos CommentMessage with the stub-returned
    external_comment_id."""
    org_id = uuid4()

    # 1. Ticket + PR rows so the aggregate FK lands.
    ext_id = f"42-{uuid4().hex[:6]}"
    ticket_id, _ = await create_ticket(
        type="pr_review",
        payload={},
        idempotency_key=ext_id,
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id=ext_id,
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="me/repo",
            external_id=f"gh-pr-{ext_id}",
            number=42,
            title="t",
            body=None,
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feature",
            base_sha="babecafe",
            head_sha="deadbeef",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="http://test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        ticket_id=ticket_id,
        org_id=org_id,
        session=db_session,
    )
    pr_id = pr.id
    await db_session.commit()

    # 2. Admit one realistic raw finding.
    raw = [_real_raw_finding()]
    admission = await admit_raw_findings(
        pr_id=pr_id,
        org_id=org_id,
        raw=raw,
        commit_sha="deadbeef",
        session=db_session,
    )
    assert len(admission.admitted) == 1, f"expected 1 admitted, got {len(admission.admitted)}"

    # 3. Register stub VCS plugin and call post_admitted_findings_to_vcs.
    with register_stub_vcs(plugin_id="github") as stub:
        post_result = await post_admitted_findings_to_vcs(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id="gh-pr-42",
            vcs_plugin_id="github",
            admitted=admission.admitted,
            raw=raw,
            summary_body="Spy summary",
            session=db_session,
        )
        await db_session.commit()

        # 4. The stub recorded a post_review call.
        assert len(stub.posted_reviews) == 1
        external_id, posted_review = stub.posted_reviews[0]
        assert external_id == "gh-pr-42"
        assert posted_review.agent_tag == "yaaos"
        assert posted_review.state == "COMMENT"
        assert len(posted_review.findings) == 1

    # 5. CommentMessageRow lands with the stub-returned external_comment_id.
    finding_row = (await db_session.execute(select(FindingRow).where(FindingRow.pr_id == pr_id))).scalar_one()
    assert finding_row.id is not None

    expected_external_id = next(iter(post_result.finding_to_comment_external_id.values()))
    msgs = (
        (
            await db_session.execute(
                select(CommentMessageRow).where(CommentMessageRow.external_comment_id == expected_external_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 1
    assert msgs[0].author_kind == "yaaos"
