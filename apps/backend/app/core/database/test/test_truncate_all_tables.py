"""Service test for ``core/database.truncate_all_tables``."""

from __future__ import annotations

import pytest

from app.core.auth import Role
from app.core.database import truncate_all_tables
from app.core.identity import create_user
from app.domain.orgs import create_membership, create_org, get_org


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

    # Confirm org exists before truncation.
    assert await get_org(org.id) is not None

    # Truncate — must commit so the DDL flushes.
    await truncate_all_tables(db_session)
    await db_session.commit()

    # The org row is gone.
    assert await get_org(org.id) is None


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
