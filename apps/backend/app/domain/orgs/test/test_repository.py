"""Repository-level smoke tests for `domain/orgs` against real Postgres.

Org and membership CRUD is owned by `core/tenancy`. This file tests the
`domain/orgs/repository` shims plus the invitation helpers that `domain/orgs`
still owns outright.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.auth import Role
from app.core.identity import repository as identity_repo
from app.core.tenancy import get_membership_info, update_org_fields
from app.domain.orgs import repository as repo


@pytest.mark.asyncio
async def test_create_org_and_owner_membership(db_session) -> None:
    org = await repo.insert_org(db_session, slug="acme", display_name="Acme")
    assert org.slug == "acme"

    user = await identity_repo.insert_user(db_session, display_name="Owner")
    await repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.OWNER, handle="owner"
    )

    found = await repo.get_membership(db_session, user_id=user.id, org_id=org.org_id)
    assert found is not None
    assert found.role == Role.OWNER


@pytest.mark.asyncio
async def test_unique_handle_per_org(db_session) -> None:
    org = await repo.insert_org(db_session, slug="dup-handle")
    a = await identity_repo.insert_user(db_session)
    b = await identity_repo.insert_user(db_session)
    await repo.insert_membership(db_session, user_id=a.id, org_id=org.org_id, role=Role.OWNER, handle="jack")
    with pytest.raises(IntegrityError):
        await repo.insert_membership(
            db_session, user_id=b.id, org_id=org.org_id, role=Role.BUILDER, handle="jack"
        )


@pytest.mark.asyncio
async def test_role_covers_ordering() -> None:
    assert Role.OWNER.covers(Role.BUILDER)
    assert Role.OWNER.covers(Role.OWNER)
    assert Role.ADMIN.covers(Role.BUILDER)
    assert not Role.BUILDER.covers(Role.OWNER)
    assert not Role.BUILDER.covers(Role.ADMIN)


@pytest.mark.asyncio
async def test_update_role(db_session) -> None:
    org = await repo.insert_org(db_session, slug="role-change")
    user = await identity_repo.insert_user(db_session)
    await repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="m"
    )
    await repo.update_role(db_session, user_id=user.id, org_id=org.org_id, role=Role.ADMIN)
    info = await get_membership_info(db_session, user_id=user.id, org_id=org.org_id)
    assert info is not None
    assert info.role == Role.ADMIN


@pytest.mark.asyncio
async def test_invitation_persisted_with_token_hash(db_session) -> None:
    org = await repo.insert_org(db_session, slug="invites")
    token_hash = repo.hash_token("rawtoken")
    inv = await repo.insert_invitation(
        db_session,
        org_id=org.org_id,
        email="invitee@example.com",
        role=Role.BUILDER,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=7),
        invited_by_user_id=None,
    )
    assert inv.token_hash == token_hash
    fetched = await repo.get_invitation_by_token_hash(db_session, token_hash)
    assert fetched is not None and fetched.email == "invitee@example.com"


@pytest.mark.asyncio
async def test_get_org_by_slug_excludes_archived(db_session) -> None:
    org = await repo.insert_org(db_session, slug="will-archive")
    assert await repo.get_org_by_slug(db_session, "will-archive") is not None
    await update_org_fields(db_session, org.org_id, archived_at=datetime.now(UTC))
    await db_session.flush()
    # get_org_by_slug filters out archived rows.
    assert await repo.get_org_by_slug(db_session, "will-archive") is None
