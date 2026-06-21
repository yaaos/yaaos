"""Service tests for `domain/orgs.list_active_member_ids`."""

from __future__ import annotations

import pytest

from app.core.auth import Role
from app.core.identity import insert_user
from app.domain.orgs import list_active_member_ids
from app.domain.orgs import repository as orgs_repo


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_active_member_ids_returns_active_only(db_session) -> None:
    """Seed two orgs with a mix of members; only the requested org's members
    are returned."""
    org_a = await orgs_repo.insert_org(db_session, slug="lam-org-a")
    org_b = await orgs_repo.insert_org(db_session, slug="lam-org-b")

    # Three users in org_a.
    user1 = await insert_user(db_session, display_name="User1")
    user2 = await insert_user(db_session, display_name="User2")
    user3 = await insert_user(db_session, display_name="User3")

    # One user only in org_b — must not appear in org_a results.
    user_b = await insert_user(db_session, display_name="UserB")

    await orgs_repo.insert_membership(
        db_session, user_id=user1.id, org_id=org_a.org_id, role=Role.OWNER, handle="u1"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user2.id, org_id=org_a.org_id, role=Role.ADMIN, handle="u2"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user3.id, org_id=org_a.org_id, role=Role.BUILDER, handle="u3"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user_b.id, org_id=org_b.org_id, role=Role.OWNER, handle="ub"
    )

    await db_session.flush()

    result = await list_active_member_ids(org_a.org_id, session=db_session)

    assert set(result) == {user1.id, user2.id, user3.id}
    assert user_b.id not in result


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_active_member_ids_empty_org(db_session) -> None:
    """An org with no members returns an empty list."""
    org = await orgs_repo.insert_org(db_session, slug="lam-empty")
    await db_session.flush()

    result = await list_active_member_ids(org.org_id, session=db_session)

    assert result == []
