"""Invitation + membership lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.domain.orgs import (
    InvitationExpiredError,
    InvitationInvalidError,
    InvitationUsedError,
    accept_invitation,
    change_role,
    invite,
    remove_member,
)
from app.domain.orgs import repository as orgs_repo
from app.testing.seed import read_email_inbox


async def _bootstrap_org_and_owner(db):
    org = await orgs_repo.insert_org(db, slug="acme-inv")
    owner = await identity_repo.insert_user(db, display_name="Owner")
    await orgs_repo.insert_membership(
        db, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="owner"
    )
    return org, owner


@pytest.mark.asyncio
async def test_invite_happy_path(db_session) -> None:
    org, owner = await _bootstrap_org_and_owner(db_session)
    invitation, raw = await invite(
        db_session,
        org_id=org.org_id,
        email="Newbie@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=Actor.user(user_id=owner.id),
    )
    assert invitation.email == "newbie@example.com"
    assert invitation.role == Role.BUILDER
    assert raw  # raw signed token returned to caller (tests + email body)
    inbox = read_email_inbox()
    assert len(inbox) == 1
    assert inbox[0].to == "Newbie@example.com"
    assert raw in inbox[0].body


@pytest.mark.asyncio
async def test_accept_invitation_creates_membership(db_session) -> None:
    org, owner = await _bootstrap_org_and_owner(db_session)
    _, raw = await invite(
        db_session,
        org_id=org.org_id,
        email="bob@example.com",
        role=Role.ADMIN,
        invited_by_user_id=owner.id,
        actor=Actor.user(user_id=owner.id),
    )
    bob = await identity_repo.insert_user(db_session, display_name="Bob")

    membership = await accept_invitation(
        db_session,
        raw_token=raw,
        user_id=bob.id,
        actor=Actor.user(user_id=bob.id),
    )
    assert membership.user_id == bob.id
    assert membership.role == Role.ADMIN


@pytest.mark.asyncio
async def test_accept_used_invitation_raises_used(db_session) -> None:
    org, owner = await _bootstrap_org_and_owner(db_session)
    _, raw = await invite(
        db_session,
        org_id=org.org_id,
        email="c@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=Actor.user(user_id=owner.id),
    )
    user = await identity_repo.insert_user(db_session)
    await accept_invitation(db_session, raw_token=raw, user_id=user.id, actor=Actor.user(user_id=user.id))

    with pytest.raises(InvitationUsedError):
        await accept_invitation(db_session, raw_token=raw, user_id=user.id, actor=Actor.user(user_id=user.id))


@pytest.mark.asyncio
async def test_accept_expired_invitation_raises_expired(db_session) -> None:
    org, owner = await _bootstrap_org_and_owner(db_session)
    _, raw = await invite(
        db_session,
        org_id=org.org_id,
        email="d@example.com",
        role=Role.BUILDER,
        invited_by_user_id=owner.id,
        actor=Actor.user(user_id=owner.id),
    )
    # Force the row to be expired without sleeping.
    from sqlalchemy import update  # noqa: PLC0415

    from app.domain.orgs.models import InvitationRow  # noqa: PLC0415

    await db_session.execute(
        update(InvitationRow)
        .where(InvitationRow.email == "d@example.com")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    user = await identity_repo.insert_user(db_session)
    with pytest.raises(InvitationExpiredError):
        await accept_invitation(db_session, raw_token=raw, user_id=user.id, actor=Actor.user(user_id=user.id))


@pytest.mark.asyncio
async def test_accept_garbage_token_raises_invalid(db_session) -> None:
    user = await identity_repo.insert_user(db_session)
    with pytest.raises(InvitationInvalidError):
        await accept_invitation(
            db_session,
            raw_token="not-a-signed-token",
            user_id=user.id,
            actor=Actor.user(user_id=user.id),
        )


@pytest.mark.asyncio
async def test_remove_member_revokes_sessions(db_session) -> None:
    org, owner = await _bootstrap_org_and_owner(db_session)
    target = await identity_repo.insert_user(db_session)
    await orgs_repo.insert_membership(
        db_session, user_id=target.id, org_id=org.org_id, role=Role.BUILDER, handle="t"
    )
    s1 = await session_lifecycle.create(db_session, user_id=target.id, workspace_id=None)
    s2 = await session_lifecycle.create(db_session, user_id=target.id, workspace_id=None)

    await remove_member(db_session, org_id=org.org_id, user_id=target.id, actor=Actor.user(user_id=owner.id))

    assert await orgs_repo.get_membership(db_session, user_id=target.id, org_id=org.org_id) is None
    assert await session_lifecycle.lookup(db_session, s1.raw_token) is None
    assert await session_lifecycle.lookup(db_session, s2.raw_token) is None


@pytest.mark.asyncio
async def test_change_role_rotates_sessions(db_session) -> None:
    org, owner = await _bootstrap_org_and_owner(db_session)
    target = await identity_repo.insert_user(db_session)
    await orgs_repo.insert_membership(
        db_session, user_id=target.id, org_id=org.org_id, role=Role.BUILDER, handle="t2"
    )
    s1 = await session_lifecycle.create(db_session, user_id=target.id, workspace_id=None)

    membership = await change_role(
        db_session,
        org_id=org.org_id,
        user_id=target.id,
        new_role=Role.ADMIN,
        actor=Actor.user(user_id=owner.id),
    )
    assert membership.role == Role.ADMIN
    # All prior sessions are revoked; user must re-auth.
    assert await session_lifecycle.lookup(db_session, s1.raw_token) is None
