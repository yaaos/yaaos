"""Service tests: `notifications.fanout` task handler and `service.create` invariants.

Scenarios covered:
1. `fanout` with two specs writes two notification rows.
2. Calling `fanout` twice with identical specs writes exactly two rows
   (idempotency from `service.create`'s dedup on (user_id, type, subject_type, subject_id)).
3. `fanout` invoked via the outbox drain path (enqueue → drain → task body)
   writes the same rows — verifying the durability path.
4. `create` enforces the subject_type/subject_id pair invariant.
5. Dedup keys on the subject tuple.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.auth import Role
from app.core.identity import insert_user
from app.core.notifications import NotificationSpec, fanout
from app.core.notifications.models import NotificationRow
from app.core.notifications.service import create
from app.core.notifications.tasks import _fanout
from app.core.tasks import drain_once, enqueue
from app.domain.orgs import repository as orgs_repo


@pytest_asyncio.fixture
async def seeded(db_session):
    alice = await insert_user(db_session, display_name="Alice")
    bob = await insert_user(db_session, display_name="Bob")
    org = await orgs_repo.insert_org(db_session, slug="task-org", display_name="TaskOrg")
    await orgs_repo.insert_membership(
        db_session, user_id=alice.id, org_id=org.org_id, role=Role.BUILDER, handle="alice"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=bob.id, org_id=org.org_id, role=Role.BUILDER, handle="bob"
    )
    await db_session.commit()
    yield {"alice": alice, "bob": bob, "org": org}


@pytest.mark.asyncio
@pytest.mark.service
async def test_fanout_creates_per_spec(seeded, db_session) -> None:
    """fanout with two specs writes exactly two notification rows."""
    alice_id = seeded["alice"].id
    bob_id = seeded["bob"].id
    org_id = seeded["org"].org_id
    subject_id = uuid4()

    specs = [
        NotificationSpec(
            user_id=alice_id,
            org_id=org_id,
            type="ticket_completed",
            title="Review complete",
            body="Fix the flake",
            subject_type="ticket",
            subject_id=subject_id,
        ).to_dict(),
        NotificationSpec(
            user_id=bob_id,
            org_id=org_id,
            type="ticket_completed",
            title="Review complete",
            body="Fix the flake",
            subject_type="ticket",
            subject_id=subject_id,
        ).to_dict(),
    ]
    await _fanout(specs=specs)

    rows = (
        (await db_session.execute(select(NotificationRow).where(NotificationRow.subject_id == subject_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {r.user_id for r in rows} == {alice_id, bob_id}
    for r in rows:
        assert r.type == "ticket_completed"
        assert r.subject_type == "ticket"
        assert r.org_id == org_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_fanout_is_idempotent_on_redelivery(seeded, db_session) -> None:
    """Calling fanout twice with identical specs yields exactly two rows, not four."""
    alice_id = seeded["alice"].id
    bob_id = seeded["bob"].id
    org_id = seeded["org"].org_id
    subject_id = uuid4()

    specs = [
        NotificationSpec(
            user_id=alice_id,
            org_id=org_id,
            type="hitl_waiting",
            title="Review needs input",
            body="PR X",
            subject_type="ticket",
            subject_id=subject_id,
        ).to_dict(),
        NotificationSpec(
            user_id=bob_id,
            org_id=org_id,
            type="hitl_waiting",
            title="Review needs input",
            body="PR X",
            subject_type="ticket",
            subject_id=subject_id,
        ).to_dict(),
    ]
    await _fanout(specs=specs)
    await _fanout(specs=specs)

    rows = (
        (await db_session.execute(select(NotificationRow).where(NotificationRow.subject_id == subject_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2, "idempotent: second call must not duplicate rows"
    assert {r.type for r in rows} == {"hitl_waiting"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_fanout_durability_via_outbox(seeded, db_session) -> None:
    """fanout survives the outbox drain path (enqueue → drain → body)."""
    alice_id = seeded["alice"].id
    bob_id = seeded["bob"].id
    org_id = seeded["org"].org_id
    subject_id = uuid4()

    specs = [
        NotificationSpec(
            user_id=alice_id,
            org_id=org_id,
            type="ticket_failed",
            title="Review failed",
            body="PR Y",
            subject_type="ticket",
            subject_id=subject_id,
        ).to_dict(),
        NotificationSpec(
            user_id=bob_id,
            org_id=org_id,
            type="ticket_failed",
            title="Review failed",
            body="PR Y",
            subject_type="ticket",
            subject_id=subject_id,
        ).to_dict(),
    ]

    await enqueue(
        fanout,
        args={"specs": specs},
        metadata={"org_id": str(org_id)},
        session=db_session,
    )
    await db_session.commit()

    async def _dispatcher(kind: str, payload: dict[str, Any]) -> None:
        assert kind == "taskiq_enqueue"
        await _fanout(specs=payload["args"]["specs"])

    delivered = await drain_once(db_session, dispatcher=_dispatcher)
    await db_session.commit()

    assert delivered == 1, "drain must have dispatched the outbox row"

    rows = (
        (await db_session.execute(select(NotificationRow).where(NotificationRow.subject_id == subject_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {r.user_id for r in rows} == {alice_id, bob_id}
    assert {r.type for r in rows} == {"ticket_failed"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_enforces_subject_pair(seeded, db_session) -> None:
    """create() raises when exactly one of subject_type/subject_id is set."""
    alice_id = seeded["alice"].id
    org_id = seeded["org"].org_id

    with pytest.raises(ValueError, match="both be null or both be set"):
        await create(
            user_id=alice_id,
            org_id=org_id,
            type="ticket_completed",
            title="X",
            body="Y",
            subject_type="ticket",
            subject_id=None,  # missing partner
            session=db_session,
        )

    with pytest.raises(ValueError, match="both be null or both be set"):
        await create(
            user_id=alice_id,
            org_id=org_id,
            type="ticket_completed",
            title="X",
            body="Y",
            subject_type=None,  # missing partner
            subject_id=uuid4(),
            session=db_session,
        )


@pytest.mark.asyncio
@pytest.mark.service
async def test_dedup_on_subject_tuple(seeded, db_session) -> None:
    """create() deduplicates on (user_id, type, subject_type, subject_id)."""
    alice_id = seeded["alice"].id
    org_id = seeded["org"].org_id
    subject_id = uuid4()

    first = await create(
        user_id=alice_id,
        org_id=org_id,
        type="ticket_completed",
        title="X",
        body="Y",
        subject_type="ticket",
        subject_id=subject_id,
        session=db_session,
    )
    second = await create(
        user_id=alice_id,
        org_id=org_id,
        type="ticket_completed",
        title="X (re-emit)",
        body="Y (re-emit)",
        subject_type="ticket",
        subject_id=subject_id,
        session=db_session,
    )
    # Different subject_id — must NOT be deduplicated.
    third = await create(
        user_id=alice_id,
        org_id=org_id,
        type="ticket_completed",
        title="X",
        body="Y",
        subject_type="ticket",
        subject_id=uuid4(),
        session=db_session,
    )
    await db_session.commit()

    assert first is not None
    assert second is None  # idempotent no-op
    assert third is not None  # different subject — new row
