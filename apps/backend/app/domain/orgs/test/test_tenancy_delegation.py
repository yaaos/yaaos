"""Service tests verifying domain/orgs delegates org/membership state to core/tenancy."""

from __future__ import annotations

import pytest

from app.core.auth import Role
from app.core.identity import create_user
from app.core.tenancy import get_membership_info, get_org_full
from app.domain.orgs import create_membership, create_org, insert_org
from app.domain.orgs.sso import upsert_config


@pytest.mark.asyncio
@pytest.mark.service
async def test_orgs_create_delegates_to_tenancy(db_session) -> None:
    """domain/orgs.create_org writes the org row via core/tenancy — get_org_full
    sees the inserted row without any direct OrgRow import in domain/orgs."""
    org = await create_org(db_session, slug="tenancy-delegation-org", display_name="Delegation Test")
    await db_session.commit()

    # Confirm via tenancy — not via a direct OrgRow select.
    full = await get_org_full(db_session, org.id)
    assert full is not None
    assert full.slug == "tenancy-delegation-org"
    assert full.display_name == "Delegation Test"

    # create_membership also goes through tenancy.
    user = await create_user(db_session, display_name="Member")
    await create_membership(db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="mem")
    await db_session.commit()

    info = await get_membership_info(db_session, user_id=user.id, org_id=org.id)
    assert info is not None
    assert info.role == Role.BUILDER
    assert info.handle == "mem"


@pytest.mark.asyncio
@pytest.mark.service
async def test_sso_set_authz_via_tenancy(db_session) -> None:
    """upsert_config writes SSO authz flags via core/tenancy.set_sso_authz_for_org
    — resolve_auth_org sees sso_enabled=True after the config is upserted."""
    from app.core.tenancy import resolve_auth_org  # noqa: PLC0415

    org = await insert_org(db_session, slug="sso-authz-org")
    user = await create_user(db_session, display_name="Owner")
    await create_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.OWNER, handle="own")
    await db_session.commit()

    # Before enabling SSO, sso_enabled is False.
    auth = await resolve_auth_org(db_session, user_id=user.id, slug=org.slug)
    assert auth is not None
    assert auth.sso_enabled is False

    # Enable SSO via domain/orgs.sso.upsert_config — writes via core/tenancy.
    await upsert_config(
        db_session,
        org_id=org.org_id,
        idp_metadata_xml="<md/>",
        enabled=True,
        exempt_owner_user_id=None,
        email_domains=["example.com"],
    )
    await db_session.commit()

    # resolve_auth_org sees the updated SSO flag via core/tenancy.
    auth_after = await resolve_auth_org(db_session, user_id=user.id, slug=org.slug)
    assert auth_after is not None
    assert auth_after.sso_enabled is True
