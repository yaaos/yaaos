"""Service test: publish_findings inserts a Review row without a run_id column.

Verifies that the new signature (no `run_id` parameter) works end-to-end:
- `publish_findings` succeeds and returns the expected (Review, []) pair.
- The `reviews` table does not have a `run_id` column (schema check).
- The returned `Review` has the correct shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.vcs import VCSPullRequest
from app.domain.reviewer import publish_findings
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import upsert as upsert_pr
from app.testing.stub_vcs import register_stub_vcs


async def _seed_pr(db_session):  # type: ignore[no-untyped-def]
    """Seed an org + ticket + PR row so publish_findings can insert a Review row."""
    org_id = uuid.uuid4()
    ext_id = f"no-run-id-{uuid.uuid4().hex[:6]}"
    ticket_id, _created = await create_ticket(
        org_id=org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=ext_id,
        payload={"head_sha": "deadbeef"},
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="me/repo",
            external_id=f"pr-{ext_id}",
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
    await db_session.commit()
    return org_id, pr.id, f"pr-{ext_id}", "github"


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_findings_without_run_id_inserts_review(db_session) -> None:  # type: ignore[no-untyped-def]
    """publish_findings succeeds with the new signature (no run_id param).

    Uses zero findings so the VCS post_finding path is not exercised — no
    VCS plugin needed.
    """
    org_id, pr_id, pr_external_id, vcs_plugin_id = await _seed_pr(db_session)

    with register_stub_vcs(plugin_id="github"):
        review, admitted = await publish_findings(
            pr_id=pr_id,
            org_id=org_id,
            pr_external_id=pr_external_id,
            vcs_plugin_id=vcs_plugin_id,
            findings=[],
            session=db_session,
        )

    assert admitted == []
    assert review.pr_id == pr_id
    assert review.org_id == org_id
    assert review.status == "done"

    # Verify the review row was inserted and that the reviews table has no
    # run_id column (schema-level guard that the migration ran correctly).
    result = await db_session.execute(
        text("SELECT id FROM reviews WHERE id = :id"),
        {"id": review.id},
    )
    row = result.one_or_none()
    assert row is not None, "review row must be persisted"

    # Confirm run_id column is absent from the schema.
    col_result = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'reviews' AND column_name = 'run_id'"
        )
    )
    assert col_result.one_or_none() is None, "reviews.run_id column must not exist"
