"""Service tests for `core/tenancy`.

Scenarios:
1. `resolve_auth_org` returns AuthOrg with correct role + SSO flags.
2. `list_memberships_for_user` returns MembershipView records with org_name + handle.
3. `create_org` + `create_membership` round-trip via tenancy primitives only.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.auth import Role
from app.core.identity import create_user
from app.core.tenancy import (
    AuthOrg,
    MembershipView,
    OrgRef,
    create_membership,
    create_org,
    list_memberships_for_user,
    resolve_auth_org,
)


@pytest_asyncio.fixture
async def seeded(db_session):
    alice = await create_user(db_session, display_name="Alice")
    bob = await create_user(db_session, display_name="Bob")
    org = await create_org(db_session, slug="tenancy-test-org", display_name="Tenancy Test Org")
    await create_membership(
        db_session,
        user_id=alice.id,
        org_id=org.org_id,
        role=Role.OWNER,
        handle="alice",
    )
    await create_membership(
        db_session,
        user_id=bob.id,
        org_id=org.org_id,
        role=Role.BUILDER,
        handle="bob",
    )
    await db_session.commit()
    yield {"alice": alice, "bob": bob, "org": org}


@pytest.mark.asyncio
@pytest.mark.service
async def test_tenancy_resolve_auth_org(seeded, db_session) -> None:
    """resolve_auth_org returns AuthOrg with the correct role and SSO defaults."""
    alice = seeded["alice"]
    org: OrgRef = seeded["org"]

    auth_org = await resolve_auth_org(db_session, user_id=alice.id, slug=org.slug)

    assert auth_org is not None
    assert isinstance(auth_org, AuthOrg)
    assert auth_org.org_id == org.org_id
    assert auth_org.slug == org.slug
    assert auth_org.role == Role.OWNER
    # sso_enabled defaults to False until set via set_sso_authz_for_org.
    assert auth_org.sso_enabled is False
    assert auth_org.sso_exempt_owner_user_id is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_tenancy_resolve_auth_org_missing_membership(seeded, db_session) -> None:
    """resolve_auth_org returns None when the user has no membership."""
    org: OrgRef = seeded["org"]
    stranger = await create_user(db_session, display_name="Stranger")

    result = await resolve_auth_org(db_session, user_id=stranger.id, slug=org.slug)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_tenancy_resolve_auth_org_unknown_slug(seeded, db_session) -> None:
    """resolve_auth_org returns None for a non-existent slug."""
    alice = seeded["alice"]
    result = await resolve_auth_org(db_session, user_id=alice.id, slug="does-not-exist")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_memberships_returns_views(seeded, db_session) -> None:
    """list_memberships_for_user returns MembershipView with org_name and handle."""
    alice = seeded["alice"]
    org: OrgRef = seeded["org"]

    views = await list_memberships_for_user(db_session, alice.id)

    assert len(views) == 1
    view = views[0]
    assert isinstance(view, MembershipView)
    assert view.org_id == org.org_id
    assert view.slug == org.slug
    assert view.org_name == "Tenancy Test Org"
    assert view.role == Role.OWNER
    assert view.handle == "alice"


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_memberships_multiple_orgs(db_session) -> None:
    """list_memberships_for_user returns one view per org membership."""
    user = await create_user(db_session, display_name="MultiOrg")
    org_a = await create_org(db_session, slug="multi-a", display_name="Org A")
    org_b = await create_org(db_session, slug="multi-b", display_name="Org B")
    await create_membership(db_session, user_id=user.id, org_id=org_a.org_id, role=Role.ADMIN, handle="u-a")
    await create_membership(db_session, user_id=user.id, org_id=org_b.org_id, role=Role.BUILDER, handle="u-b")
    await db_session.commit()

    views = await list_memberships_for_user(db_session, user.id)

    assert len(views) == 2
    slugs = {v.slug for v in views}
    assert slugs == {"multi-a", "multi-b"}
    handles = {v.handle for v in views}
    assert handles == {"u-a", "u-b"}


@pytest.mark.asyncio
@pytest.mark.service
async def test_create_org_membership_via_tenancy(db_session) -> None:
    """create_org + create_membership produce a resolvable AuthOrg."""
    user = await create_user(db_session, display_name="Owner")
    org = await create_org(db_session, slug="new-via-tenancy", display_name="New Org")

    assert isinstance(org, OrgRef)
    assert org.slug == "new-via-tenancy"
    assert org.name == "New Org"

    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=Role.OWNER,
        handle="owner-handle",
    )
    await db_session.commit()

    auth_org = await resolve_auth_org(db_session, user_id=user.id, slug="new-via-tenancy")
    assert auth_org is not None
    assert auth_org.role == Role.OWNER
    assert auth_org.org_id == org.org_id
