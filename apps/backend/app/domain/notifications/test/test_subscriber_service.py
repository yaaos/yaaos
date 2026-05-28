"""Service test for the notifications writer.

Asserts that a ticket status transition into hitl/done/failed writes one
notification row per member of the ticket's org, via the same handler
the live subscriber uses. The asyncio plumbing (background task +
subscribe-from-bus) is not exercised here — the conftest's transactional
session model can't be safely shared across tasks. The handler itself
runs synchronously inside the test's session.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.domain.identity import repository as identity_repo
from app.domain.notifications.models import NotificationRow
from app.domain.notifications.subscribers import _handle_status_change
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo
from app.domain.tickets import TicketStatusChanged


@pytest_asyncio.fixture
async def seeded(db_session):
    alice = await identity_repo.insert_user(db_session, display_name="Alice")
    bob = await identity_repo.insert_user(db_session, display_name="Bob")
    org = await orgs_repo.insert_org(db_session, slug="sub-org", display_name="SubOrg")
    await orgs_repo.insert_membership(
        db_session, user_id=alice.id, org_id=org.id, role=Role.BUILDER, handle="alice"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=bob.id, org_id=org.id, role=Role.BUILDER, handle="bob"
    )

    ticket_id = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status, plugin_id,"
            " repo_external_id) VALUES (:id, :org_id, 'github_pr', 'x/y#1', 'Tighten retries',"
            " 'running', 'github', 'x/y')"
        ),
        {"id": ticket_id, "org_id": org.id},
    )
    await db_session.commit()
    yield {"alice": alice, "bob": bob, "org": org, "ticket_id": ticket_id}


@pytest.mark.service
@pytest.mark.asyncio
async def test_status_change_to_hitl_writes_rows_for_every_member(seeded, db_session) -> None:
    await _handle_status_change(
        TicketStatusChanged(
            ticket_id=seeded["ticket_id"],
            repo_external_id="x/y",
            pr_id=None,
            previous_status="running",
            new_status="hitl",
        )
    )

    rows = (
        (
            await db_session.execute(
                select(NotificationRow).where(NotificationRow.ticket_id == seeded["ticket_id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    user_ids = {r.user_id for r in rows}
    assert user_ids == {seeded["alice"].id, seeded["bob"].id}
    for r in rows:
        assert r.type == "hitl_waiting"
        assert r.title == "Reviewer needs your input"
        assert r.body == "Tighten retries"
        assert r.org_id == seeded["org"].id


@pytest.mark.service
@pytest.mark.asyncio
async def test_status_change_to_done_writes_ticket_completed(seeded, db_session) -> None:
    await _handle_status_change(
        TicketStatusChanged(
            ticket_id=seeded["ticket_id"],
            repo_external_id="x/y",
            pr_id=None,
            previous_status="running",
            new_status="done",
        )
    )

    rows = (
        (
            await db_session.execute(
                select(NotificationRow).where(NotificationRow.ticket_id == seeded["ticket_id"])
            )
        )
        .scalars()
        .all()
    )
    assert {r.type for r in rows} == {"ticket_completed"}
    assert {r.title for r in rows} == {"Review complete"}


@pytest.mark.service
@pytest.mark.asyncio
async def test_running_status_does_not_write_a_notification(seeded, db_session) -> None:
    """The subscriber only writes for hitl / done / failed — `running`
    (creation transition) is noise and must not produce a notification."""
    await _handle_status_change(
        TicketStatusChanged(
            ticket_id=seeded["ticket_id"],
            repo_external_id="x/y",
            pr_id=None,
            previous_status=None,
            new_status="running",
        )
    )

    rows = (
        (
            await db_session.execute(
                select(NotificationRow).where(NotificationRow.ticket_id == seeded["ticket_id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0


@pytest.mark.service
@pytest.mark.asyncio
async def test_record_is_idempotent_per_user_type_ticket(seeded, db_session) -> None:
    """Re-emitting the same transition (e.g. workflow retry) must not
    double-write — service.record() keys on (user_id, type, ticket_id)."""
    event = TicketStatusChanged(
        ticket_id=seeded["ticket_id"],
        repo_external_id="x/y",
        pr_id=None,
        previous_status="running",
        new_status="hitl",
    )
    await _handle_status_change(event)
    await _handle_status_change(event)

    rows = (
        (
            await db_session.execute(
                select(NotificationRow).where(NotificationRow.ticket_id == seeded["ticket_id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2  # one row per member, not four
