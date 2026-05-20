"""Coverage for the Phase 8 audit-log helpers: `list_for_org` filters,
`purge_older_than`, and the retention constant wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import update

from app.core.audit_log import AUDIT_LOG_RETENTION, Actor, audit, list_for_org, purge_older_than
from app.core.audit_log.models import AuditEntryRow


class _Payload(BaseModel):
    note: str


def _user_actor():
    return Actor.user(user_id=uuid4())


def test_retention_is_15_days() -> None:
    # Lowered from 30d in M04 Phase 6 — MCP-dispatch audit rows are the
    # dominant volume contributor, and 15d keeps the storage envelope sane.
    assert AUDIT_LOG_RETENTION == timedelta(days=15)


@pytest.mark.asyncio
async def test_list_for_org_filters_by_actor_kind(db_session) -> None:
    org_id = uuid4()
    await audit(
        "user", uuid4(), "logged_in", _Payload(note="a"), _user_actor(), org_id=org_id, session=db_session
    )
    await audit(
        "ticket",
        uuid4(),
        "noted",
        _Payload(note="b"),
        Actor(kind="system"),
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    rows = await list_for_org(org_id=org_id, actor_kinds=["user"])
    assert {r.actor.kind.value for r in rows} == {"user"}
    assert all(r.org_id == org_id for r in rows)


@pytest.mark.asyncio
async def test_list_for_org_filters_by_action(db_session) -> None:
    org_id = uuid4()
    await audit(
        "user", uuid4(), "logged_in", _Payload(note="x"), _user_actor(), org_id=org_id, session=db_session
    )
    await audit(
        "user", uuid4(), "logout", _Payload(note="y"), _user_actor(), org_id=org_id, session=db_session
    )
    await db_session.commit()

    only_login = await list_for_org(org_id=org_id, actions=["logged_in"])
    assert all(r.kind == "logged_in" for r in only_login)


@pytest.mark.asyncio
async def test_purge_older_than_drops_old_rows(db_session) -> None:
    org_id = uuid4()
    fresh = await audit(
        "user", uuid4(), "logged_in", _Payload(note="new"), _user_actor(), org_id=org_id, session=db_session
    )
    stale = await audit(
        "user", uuid4(), "logged_in", _Payload(note="old"), _user_actor(), org_id=org_id, session=db_session
    )
    await db_session.execute(
        update(AuditEntryRow)
        .where(AuditEntryRow.id == stale.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=31))
    )
    await db_session.commit()

    purged = await purge_older_than(datetime.now(UTC) - timedelta(days=30))
    assert purged >= 1

    surviving = await list_for_org(org_id=org_id)
    ids = {r.id for r in surviving}
    assert fresh.id in ids
    assert stale.id not in ids
