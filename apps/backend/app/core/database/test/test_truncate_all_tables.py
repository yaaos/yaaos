"""Service test for ``core/database.truncate_all_tables``."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.audit_log import AuditEntryRow
from app.core.database import truncate_all_tables
from app.core.identity import create_user
from app.domain.orgs import MembershipRow, OrgRow, Role, create_membership, create_org


@pytest.mark.asyncio
@pytest.mark.service
async def test_truncate_all_tables_clears_rows(db_session) -> None:
    """After ``truncate_all_tables`` every seeded row is gone."""
    # Seed a few rows across modules.
    org = await create_org(db_session, slug="truncate-test-org", display_name="Truncate Org")
    user = await create_user(db_session, display_name="Truncate User")
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


@pytest.mark.asyncio
async def test_truncate_all_tables_raises_in_prod(monkeypatch) -> None:
    """`truncate_all_tables` is non-prod only; refuses to run under `YAAOS_ENV=prod`."""
    monkeypatch.setenv("YAAOS_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setenv("YAAOS_ENCRYPTION_KEY", "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="non-prod only"):
            await truncate_all_tables(session=None)  # type: ignore[arg-type]
    finally:
        get_settings.cache_clear()
