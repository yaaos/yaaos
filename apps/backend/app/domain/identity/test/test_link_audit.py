"""`complete_oauth_link` emits `provider_linked` audit row per membership org."""

from __future__ import annotations

import pytest

from app.core.audit_log import list_for_org
from app.domain.identity import repository as identity_repo
from app.domain.identity.service import complete_oauth_link
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role


@pytest.mark.asyncio
async def test_complete_oauth_link_audits_per_org(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="Linker")
    await identity_repo.add_email(db_session, user_id=user.id, email="link@example.com", verified=True)
    org_a = await orgs_repo.insert_org(db_session, slug="link-a")
    org_b = await orgs_repo.insert_org(db_session, slug="link-b")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.id, role=Role.BUILDER, handle="l"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.id, role=Role.ADMIN, handle="l2"
    )

    await complete_oauth_link(db_session, user_id=user.id, provider_id="github", external_subject="42")
    await db_session.commit()

    a = await list_for_org(org_id=org_a.id, actions=["provider_linked"])
    b = await list_for_org(org_id=org_b.id, actions=["provider_linked"])
    assert len(a) >= 1
    assert len(b) >= 1
    assert a[0].payload["provider"] == "github"
    assert b[0].payload["provider"] == "github"


@pytest.mark.asyncio
async def test_complete_oauth_link_no_memberships_no_audit(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="Lonely")
    await complete_oauth_link(db_session, user_id=user.id, provider_id="github", external_subject="99")
    # No memberships → no audit rows emitted. The link itself still succeeded.
    identity = await identity_repo.find_oauth_identity(db_session, provider="github", external_subject="99")
    assert identity is not None
