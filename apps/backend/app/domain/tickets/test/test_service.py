"""Service-level tests for ticket lifecycle ops.

Covers: create_from_pr (idempotency + race-safe insert),
attach_pr_to_ticket (pr_id IS NULL guard), set_workflow_execution,
list_running_older_than, and list_tickets findings rollup columns.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.tickets import (
    attach_pr_to_ticket,
    create_from_pr,
    list_running_older_than,
    list_tickets,
    set_workflow_execution,
    update_findings_summary,
)
from app.domain.tickets.service import TicketFilter, get


@pytest.mark.service
async def test_create_from_pr_creates_new(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"

    ticket_id, created = await create_from_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="My PR title",
        description="desc",
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={"head_sha": "abc123"},
        session=db_session,
    )
    await db_session.commit()

    assert created is True
    assert ticket_id is not None

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.title == "My PR title"
    assert ticket.status == "pending"
    assert ticket.pr_id is None


@pytest.mark.service
async def test_create_from_pr_idempotent(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"

    _ticket_id, created = await create_from_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR title",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert created is True

    # Second call with same (org, source, source_external_id) re-SELECTs existing row
    ticket_id2, created2 = await create_from_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="Different title",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=f"delivery-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.commit()

    # Second call sees conflict → returns existing id, created=False
    assert created2 is False
    assert ticket_id2 is not None
    assert ticket_id2 == _ticket_id


@pytest.mark.service
async def test_attach_pr_to_ticket_when_pr_id_is_null(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"
    pr_id = uuid4()

    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=pr_id, session=db_session)
    await db_session.commit()

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.pr_id == pr_id


@pytest.mark.service
async def test_attach_pr_to_ticket_no_op_when_already_set(db_session) -> None:  # type: ignore[no-untyped-def]
    """attach_pr_to_ticket WHERE pr_id IS NULL: must not overwrite an already-set pr_id."""
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"
    first_pr_id = uuid4()
    second_pr_id = uuid4()

    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=first_pr_id, session=db_session)
    await db_session.commit()

    # Second call should be a no-op (WHERE pr_id IS NULL guard)
    await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=second_pr_id, session=db_session)
    await db_session.commit()

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.pr_id == first_pr_id


@pytest.mark.service
async def test_set_workflow_execution(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"
    workflow_execution_id = uuid4()

    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    await set_workflow_execution(ticket_id, workflow_execution_id=workflow_execution_id, session=db_session)
    await db_session.commit()

    # Verify by reading the row directly
    from sqlalchemy import select  # noqa: PLC0415

    from app.domain.tickets.models import TicketRow  # noqa: PLC0415

    async with db_session.begin_nested():
        row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()
    assert row.current_workflow_execution_id == workflow_execution_id


@pytest.mark.service
async def test_list_running_older_than_filters_correctly(db_session) -> None:  # type: ignore[no-untyped-def]
    """Returns only running tickets older than cutoff; skips fresh ones."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    org_id = uuid4()

    # Stale running ticket (created 10 minutes ago)
    stale_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"r/{uuid4().hex}",
        title="stale",
        description=None,
        repo_external_id="r",
        plugin_id="github",
        idempotency_key=f"k-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert stale_id is not None
    # Back-date the stale ticket to 10 minutes ago and flip to running (create_from_pr inserts pending).
    stale_created_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.execute(
        text("UPDATE tickets SET created_at = :ts, status = 'running' WHERE id = :id"),
        {"ts": stale_created_at, "id": stale_id},
    )
    await db_session.commit()

    # Fresh running ticket with its created_at left as the DB default (NOW()).
    # The cutoff is set AFTER the stale ticket's back-dated timestamp so only
    # the stale ticket falls before it.
    fresh_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"r/{uuid4().hex}",
        title="fresh",
        description=None,
        repo_external_id="r",
        plugin_id="github",
        idempotency_key=f"k-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert fresh_id is not None

    # cutoff is set between the two tickets' timestamps: after the stale
    # ticket (created_at = 10 min ago) and before the fresh one (NOW()).
    # Use a 5-minute boundary so the stale ticket (10 min old) is definitely
    # older, and the fresh ticket (just inserted) is definitely newer.
    cutoff = datetime.now(UTC) - timedelta(minutes=5)

    results = await list_running_older_than(cutoff)
    # list_running_older_than is intentionally org-unfiltered (system sweep over
    # all orgs), so other suite tests' running tickets can appear in the global
    # result set. Scope assertions to this test's unique org so they hold
    # regardless of suite ordering or concurrent rows.
    own_org_rows = [r for r in results if r[1] == org_id]
    result_ids = {r[0] for r in own_org_rows}
    assert stale_id in result_ids
    assert fresh_id not in result_ids
    # verify pr_id slot is present (may be None for tickets without pr)
    stale_result = next(r for r in own_org_rows if r[0] == stale_id)
    assert len(stale_result) == 3  # (ticket_id, org_id, pr_id)


@pytest.mark.service
async def test_list_tickets_reads_findings_columns(db_session) -> None:  # type: ignore[no-untyped-def]
    """list_tickets reads findings_count + max_severity from the ticket row,
    and update_findings_summary writes the rollup correctly."""
    org_id = uuid4()

    ticket_id, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"myorg/repo#{uuid4().hex[:6]}",
        title="rollup PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=f"k-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    # Before any rollup write, findings_count should default to 0.
    tickets = await list_tickets(TicketFilter(), org_id=org_id)
    assert any(t.id == ticket_id for t in tickets)
    target = next(t for t in tickets if t.id == ticket_id)
    assert target.findings_count == 0
    assert target.max_severity is None

    # Write a rollup via update_findings_summary.
    await update_findings_summary(
        ticket_id,
        findings_count=3,
        max_severity="blocker",
        session=db_session,
    )
    await db_session.commit()

    # list_tickets must now reflect the persisted rollup — no live aggregation.
    tickets2 = await list_tickets(TicketFilter(), org_id=org_id)
    target2 = next(t for t in tickets2 if t.id == ticket_id)
    assert target2.findings_count == 3
    assert target2.max_severity == "blocker"

    # findings_count sort is done at DB level — verify the ordering holds.
    # Create a second ticket with zero findings.
    ticket_id2, _ = await create_from_pr(
        org_id=org_id,
        source_external_id=f"myorg/repo#{uuid4().hex[:6]}",
        title="zero findings PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=f"k-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.commit()

    tickets3 = await list_tickets(TicketFilter(sort="findings_count"), org_id=org_id)
    ids_in_order = [t.id for t in tickets3]
    assert ids_in_order.index(ticket_id) < ids_in_order.index(ticket_id2)
