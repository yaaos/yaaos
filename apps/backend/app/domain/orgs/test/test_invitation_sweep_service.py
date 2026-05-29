"""Service test: `domain/orgs` sweeps its own expired invitations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.identity import repository as identity_repo
from app.domain.orgs import delete_expired_invitations, invite
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.models import InvitationRow


@pytest.mark.service
@pytest.mark.asyncio
async def test_orgs_self_sweeps_invitations(db_session) -> None:
    """Expired uninvited invitations are purged by `delete_expired_invitations`,
    owned by `domain/orgs`. Past-expiry rows disappear; not-yet-expired rows stay."""
    from sqlalchemy import select, update  # noqa: PLC0415

    org = await orgs_repo.insert_org(db_session, slug="sweep-test-org")
    owner = await identity_repo.insert_user(db_session, display_name="Sweeper")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own"
    )
    actor = Actor.user(user_id=owner.id)

    # Create two invitations — we'll expire one of them manually.
    _, _raw_expired = await invite(
        db_session,
        org_id=org.org_id,
        email="expired@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=actor,
    )
    _, _raw_fresh = await invite(
        db_session,
        org_id=org.org_id,
        email="fresh@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=actor,
    )
    await db_session.commit()

    # Back-date the first invitation's `expires_at` so it reads as expired.
    await db_session.execute(
        update(InvitationRow)
        .where(InvitationRow.email == "expired@example.com")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()

    purged = await delete_expired_invitations()
    assert purged == 1

    remaining = (
        (await db_session.execute(select(InvitationRow).where(InvitationRow.org_id == org.org_id)))
        .scalars()
        .all()
    )
    remaining_emails = {r.email for r in remaining}
    assert remaining_emails == {"fresh@example.com"}
