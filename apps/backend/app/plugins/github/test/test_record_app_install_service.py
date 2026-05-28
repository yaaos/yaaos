"""Service test for ``github.record_app_install``."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.orgs import create_org
from app.plugins.github import record_app_install
from app.plugins.github.models import GitHubAppInstallationRow


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_app_install_persists_row(db_session) -> None:
    """Happy path: row is inserted with the given fields and ``status='active'``."""
    org = await create_org(db_session, slug="gh-install-org-1", display_name="GH Org 1")

    await record_app_install(
        db_session,
        org_id=org.id,
        install_external_id="install-abc-123",
        account_login="acme-corp",
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(GitHubAppInstallationRow).where(
                GitHubAppInstallationRow.install_external_id == "install-abc-123"
            )
        )
    ).scalar_one_or_none()

    assert row is not None
    assert row.org_id == org.id
    assert row.account_login == "acme-corp"
    assert row.status == "active"


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_app_install_custom_status(db_session) -> None:
    """``status`` argument is stored as-is when explicitly provided."""
    org = await create_org(db_session, slug="gh-install-org-2", display_name="GH Org 2")

    await record_app_install(
        db_session,
        org_id=org.id,
        install_external_id="install-suspended-1",
        account_login="acme-corp",
        status="suspended",
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(GitHubAppInstallationRow).where(
                GitHubAppInstallationRow.install_external_id == "install-suspended-1"
            )
        )
    ).scalar_one_or_none()

    assert row is not None
    assert row.status == "suspended"
