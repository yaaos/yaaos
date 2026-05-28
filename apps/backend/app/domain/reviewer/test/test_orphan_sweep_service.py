"""Orphan-sweep safeguard: `running` tickets with no review row → `failed`.

Verifies the audit row + status transition + grace window. Service-grade
because the sweep crosses tickets + reviewer + audit modules.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.core.audit_log import list_for_entity
from app.domain.identity import repository as identity_repo
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer.orphan_sweep import ORPHAN_REASON, _sweep_once
from app.domain.tickets import TicketRow


async def _seed_running_ticket(db_session, org_id, *, ext: str, age_seconds: int) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Insert a ticket row with `created_at` shifted back by `age_seconds`."""
    tid = uuid.uuid4()
    created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    await db_session.execute(
        text(
            "INSERT INTO tickets "
            "(id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id,"
            " created_at, updated_at)"
            " VALUES (:id, :org_id, 'github_pr', :ext, :title, 'running', 'github', 'x/y',"
            " :created_at, :created_at)"
        ),
        {
            "id": tid,
            "org_id": org_id,
            "ext": ext,
            "title": f"orphan-{ext}",
            "created_at": created_at,
        },
    )
    return tid


@pytest.mark.service
@pytest.mark.asyncio
async def test_sweep_flips_stale_running_ticket_to_failed(db_session) -> None:  # type: ignore[no-untyped-def]
    user = await identity_repo.insert_user(db_session, display_name="J")
    org = await orgs_repo.insert_org(db_session, slug="orphan-org")
    del user
    # Older than the 300 s default grace.
    stale = await _seed_running_ticket(db_session, org.id, ext="x/y#1", age_seconds=600)
    # Fresh row that must NOT be touched.
    fresh = await _seed_running_ticket(db_session, org.id, ext="x/y#2", age_seconds=10)
    await db_session.commit()

    failed = await _sweep_once()
    assert failed == 1

    rows = {
        r.id: r.status
        for r in (await db_session.execute(select(TicketRow).where(TicketRow.id.in_([stale, fresh]))))
        .scalars()
        .all()
    }
    assert rows[stale] == "failed"
    assert rows[fresh] == "running"

    # Audit row with the orphan reason in payload.
    audits = await list_for_entity("ticket", stale, org_id=org.id, kinds=["ticket.status_changed"])
    assert len(audits) == 1
    assert audits[0].payload.get("reason") == ORPHAN_REASON
    assert audits[0].payload.get("to_status") == "failed"


@pytest.mark.service
@pytest.mark.asyncio
async def test_sweep_skips_ticket_with_existing_review(db_session) -> None:  # type: ignore[no-untyped-def]
    """A `running` ticket whose PR already has a `reviews` row must not be touched."""
    user = await identity_repo.insert_user(db_session, display_name="J")
    org = await orgs_repo.insert_org(db_session, slug="reviewed-org")
    del user
    ticket_id = await _seed_running_ticket(db_session, org.id, ext="x/y#9", age_seconds=600)
    pr_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO pull_requests "
            "(id, org_id, plugin_id, external_id, repo_external_id, ticket_id, number, title, body,"
            " author_login, author_type, base_branch, head_branch, base_sha, head_sha, is_draft,"
            " is_fork, state, html_url)"
            " VALUES (:pr_id, :org_id, 'github', 'x/y#9', 'x/y', :tid, 9, 't', '', 'j', 'user',"
            " 'main', 'feat', 'a', 'b', false, false, 'open', 'https://example/x/y/9')"
        ),
        {"pr_id": pr_id, "org_id": org.id, "tid": ticket_id},
    )
    await db_session.execute(
        text("UPDATE tickets SET pr_id = :pr_id WHERE id = :tid"),
        {"pr_id": pr_id, "tid": ticket_id},
    )
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status)"
            " VALUES (:id, :org_id, :pr_id, 1, 'queued')"
        ),
        {"id": review_id, "org_id": org.id, "pr_id": pr_id},
    )
    await db_session.commit()

    failed = await _sweep_once()
    assert failed == 0

    n_running = (
        await db_session.execute(
            select(func.count())
            .select_from(TicketRow)
            .where(TicketRow.id == ticket_id, TicketRow.status == "running")
        )
    ).scalar_one()
    assert n_running == 1
