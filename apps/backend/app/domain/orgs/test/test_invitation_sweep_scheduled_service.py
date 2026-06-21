"""Service-tier guards for the hourly `invitation_sweep` `@scheduled` task.

Two invariants:
  - The body is registered with the taskiq broker under the public task name.
  - The sweep body purges expired invitation rows and leaves fresh ones intact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.identity import insert_user
from app.core.tasks import get_broker
from app.domain.orgs import insert_membership, insert_org, invite
from app.domain.orgs.invitation_sweeper import _sweep_once, invitation_sweep
from app.domain.orgs.models import InvitationRow

pytestmark = pytest.mark.service

_TASK_NAME = "invitation_sweep"


@pytest.mark.asyncio
async def test_invitation_sweep_task_registered_with_broker() -> None:
    """The sweep body is registered with the broker under its public task name.
    Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None
    assert invitation_sweep is not None


@pytest.mark.asyncio
async def test_invitation_sweep_body_purges_expired(db_session) -> None:
    """Drive `_sweep_once` directly — expired invitation rows are deleted."""
    org = await insert_org(db_session, slug="inv-sched-org")
    owner = await insert_user(db_session, display_name="Owner")
    await insert_membership(db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own")
    actor = Actor.user(user_id=owner.id)

    _, _raw_expired = await invite(
        db_session,
        org_id=org.org_id,
        email="expired-sched@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=actor,
    )
    _, _raw_fresh = await invite(
        db_session,
        org_id=org.org_id,
        email="fresh-sched@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=actor,
    )
    await db_session.commit()

    # Back-date one invitation so it reads as expired.
    await db_session.execute(
        update(InvitationRow)
        .where(InvitationRow.email == "expired-sched@example.com")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()

    await _sweep_once()

    remaining = (
        (await db_session.execute(select(InvitationRow).where(InvitationRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    remaining_emails = {r.email for r in remaining}
    assert remaining_emails == {"fresh-sched@example.com"}


@pytest.mark.asyncio
async def test_invitation_sweep_body_runs_idempotently() -> None:
    """Empty DB stays empty — surfaces exceptions loudly."""
    await _sweep_once()
    await _sweep_once()
