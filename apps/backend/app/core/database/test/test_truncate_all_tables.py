"""Service test for ``core/database.truncate_all_tables``."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.audit_log.models import AuditEntryRow
from app.core.database import truncate_all_tables
from app.domain.identity import service as identity_svc
from app.domain.orgs import create_membership, create_org
from app.domain.orgs.models import MembershipRow, OrgRow
from app.domain.orgs.types import Role


@pytest.mark.asyncio
@pytest.mark.service
async def test_truncate_all_tables_clears_rows(db_session) -> None:
    """After ``truncate_all_tables`` every seeded row is gone."""
    # Seed a few rows across modules.
    org = await create_org(db_session, slug="truncate-test-org", display_name="Truncate Org")
    user = await identity_svc.create_user(db_session, display_name="Truncate User")
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.id,
        role=Role.OWNER,
        handle="truncowner",
    )
    await db_session.commit()

    # Confirm they exist before truncation.
    org_count_before = (await db_session.execute(select(func.count()).select_from(OrgRow))).scalar_one()
    assert org_count_before >= 1

    # Truncate — must commit so the DDL flushes.
    await truncate_all_tables(db_session)
    await db_session.commit()

    # Every seeded table must now be empty.
    org_count_after = (await db_session.execute(select(func.count()).select_from(OrgRow))).scalar_one()
    membership_count_after = (
        await db_session.execute(select(func.count()).select_from(MembershipRow))
    ).scalar_one()
    audit_count_after = (
        await db_session.execute(select(func.count()).select_from(AuditEntryRow))
    ).scalar_one()

    assert org_count_after == 0
    assert membership_count_after == 0
    assert audit_count_after == 0
