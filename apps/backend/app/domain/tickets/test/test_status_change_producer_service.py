"""Service tests: ticket status-change producer writes to both outbox and SSE.

Covers:
- _transition enqueues a `notifications.fanout` outbox row with the correct
  NotificationSpec list for terminal status changes.
- _transition fires a general SSE event after commit with kind
  "ticket_status_changed".
- _transition rollback does not emit any SSE event.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.core.tasks import get_pending_outbox_payloads

# ---------------------------------------------------------------------------
# Helpers — create org+users+memberships using public domain APIs
# ---------------------------------------------------------------------------


async def _make_org_with_members(db_session, num_members: int = 2):  # type: ignore[no-untyped-def]
    """Insert an org + users + memberships via public APIs.

    Returns (org_id, [user_id, ...]).
    """
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import create_user  # noqa: PLC0415
    from app.domain.orgs import create_membership, create_org  # noqa: PLC0415

    slug = f"test-org-{uuid4().hex[:8]}"
    org = await create_org(db_session, slug=slug, display_name="Test Org")
    await db_session.flush()

    user_ids = []
    for i in range(num_members):
        user = await create_user(db_session, display_name=f"Member {i}")
        await db_session.flush()
        await create_membership(
            db_session,
            user_id=user.id,
            org_id=org.id,
            role=Role.BUILDER,
            handle=f"member{i}-{uuid4().hex[:4]}",
        )
        user_ids.append(user.id)

    await db_session.commit()
    return org.id, user_ids


async def _make_ticket(db_session, org_id):  # type: ignore[no-untyped-def]
    """Insert a `running` ticket for `org_id`. Returns ticket_id."""
    from app.domain.tickets import upsert_ticket_for_pr  # noqa: PLC0415

    ticket_id, created = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=f"repo/r#{uuid4().hex[:6]}",
        title="Test PR",
        description=None,
        repo_external_id="repo/r",
        plugin_id="github",
        idempotency_key=f"delivery-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    assert created is True
    await db_session.commit()
    assert ticket_id is not None
    return ticket_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
async def test_status_change_enqueues_fanout_specs(db_session) -> None:  # type: ignore[no-untyped-def]
    """complete() must enqueue one `notifications.fanout` outbox row whose
    specs list contains one entry per active org member."""
    org_id, user_ids = await _make_org_with_members(db_session, num_members=2)
    ticket_id = await _make_ticket(db_session, org_id)

    from app.domain.tickets import complete  # noqa: PLC0415

    await complete(ticket_id, org_id=org_id)

    # The outbox row is committed inside _transition's own session; query the
    # test session (same underlying connection via set_test_session_override).
    payloads = await get_pending_outbox_payloads(db_session)
    fanout_payloads = [p for p in payloads if p.get("task_name") == "notifications.fanout"]

    assert len(fanout_payloads) == 1, f"expected exactly 1 outbox row, got {len(fanout_payloads)}"
    specs = fanout_payloads[0]["args"]["specs"]
    assert len(specs) == len(user_ids), f"expected {len(user_ids)} specs, got {len(specs)}"

    enqueued_user_ids = {s["user_id"] for s in specs}
    expected_ids = {str(u) for u in user_ids}
    assert enqueued_user_ids == expected_ids, f"user ids mismatch: {enqueued_user_ids} != {expected_ids}"

    for spec in specs:
        assert spec["subject_type"] == "ticket"
        assert spec["subject_id"] == str(ticket_id)
        assert spec["type"] == "ticket_completed"


@pytest.mark.service
@pytest.mark.asyncio
async def test_status_change_publishes_general_after_commit(db_session, redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """complete() publishes a general SSE event with kind 'ticket_status_changed'
    after the transaction commits."""
    from app.core.redis import reset_pubsub  # noqa: PLC0415
    from app.core.sse import subscribe_general  # noqa: PLC0415

    reset_pubsub()
    try:
        org_id, _ = await _make_org_with_members(db_session, num_members=1)
        ticket_id = await _make_ticket(db_session, org_id)

        received: list[dict] = []

        async def _consume() -> None:
            async for event in subscribe_general(org_id):
                received.append(event)
                return

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)  # let Redis subscription register

        from app.domain.tickets import complete  # noqa: PLC0415

        await complete(ticket_id, org_id=org_id)

        await asyncio.wait_for(consumer, timeout=3.0)

        assert len(received) == 1
        evt = received[0]
        assert evt["kind"] == "ticket_status_changed"
        assert evt["ticket_id"] == str(ticket_id)
        assert evt["new_status"] == "done"
        assert "ts" in evt
    finally:
        reset_pubsub()


@pytest.mark.service
@pytest.mark.asyncio
async def test_status_change_rollback_emits_no_general_event(db_session, redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """When publish_general_after_commit is called then the session is rolled back,
    no SSE event must reach subscribers."""
    from app.core.redis import reset_pubsub  # noqa: PLC0415
    from app.core.sse import (  # noqa: PLC0415
        GeneralEventKind,
        publish_general_after_commit,
        subscribe_general,
    )

    reset_pubsub()
    try:
        org_id = uuid4()
        received: list[dict] = []

        async def _consume() -> None:
            async for event in subscribe_general(org_id):
                received.append(event)
                return

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)

        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": str(uuid4()), "new_status": "done"},
        )
        await db_session.rollback()

        await asyncio.sleep(0.2)

        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

        assert received == [], "rollback must not emit SSE events"
    finally:
        reset_pubsub()
