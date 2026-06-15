"""Service test: `upsert_installation` is atomically idempotent under
concurrent callers for the same `install_external_id`.

Exactly one concurrent call returns True (was_insert); the other returns False.
Exactly one `github_app_installations` row exists after both commits.

Uses independent sessions off the live engine (not the db_session fixture) so
the concurrent inserts actually race on the real Postgres unique constraint.
Data is cleaned up in the fixture so the test is self-contained.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import get_sessionmaker
from app.plugins.github.models import GitHubAppInstallationRow
from app.plugins.github.service import upsert_installation

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


async def _clean(external_ids: list[str]) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(
            delete(GitHubAppInstallationRow).where(
                GitHubAppInstallationRow.install_external_id.in_(external_ids)
            )
        )
        await s.commit()


@pytest_asyncio.fixture
async def _clean_installs() -> AsyncIterator[list[str]]:
    external_ids: list[str] = []
    yield external_ids
    await _clean(external_ids)


async def test_upsert_installation_concurrent_same_key(
    _migrated_schema: None,
    _clean_installs: list[str],
) -> None:
    """Two concurrent upsert_installation calls with the same install_external_id
    produce exactly one DB row; exactly one caller receives True (was_insert) and
    the other receives False."""
    install_external_id = f"test-install-{uuid4().hex[:8]}"
    _clean_installs.append(install_external_id)
    org_id = uuid4()

    result_a, result_b = await asyncio.gather(
        upsert_installation(
            install_external_id=install_external_id,
            account_login="org-a",
            org_id=org_id,
        ),
        upsert_installation(
            install_external_id=install_external_id,
            account_login="org-a",
            org_id=org_id,
        ),
    )

    # Exactly one call performs the insert.
    assert (result_a, result_b) in {(True, False), (False, True)}, (
        f"Expected one True and one False, got ({result_a}, {result_b})"
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    select(GitHubAppInstallationRow).where(
                        GitHubAppInstallationRow.install_external_id == install_external_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"Expected exactly 1 installation row, found {len(rows)}"


async def test_upsert_installation_second_call_returns_false(
    _migrated_schema: None,
    _clean_installs: list[str],
) -> None:
    """A second sequential upsert_installation call for the same key returns False,
    confirming the was_insert semantics hold on ordinary (non-concurrent) re-upserts."""
    install_external_id = f"test-install-{uuid4().hex[:8]}"
    _clean_installs.append(install_external_id)
    org_id = uuid4()

    first = await upsert_installation(
        install_external_id=install_external_id,
        account_login="org-a",
        org_id=org_id,
    )
    second = await upsert_installation(
        install_external_id=install_external_id,
        account_login="org-a",
        org_id=org_id,
    )

    assert first is True, "First call should be the inserter"
    assert second is False, "Second call should be the updater"
