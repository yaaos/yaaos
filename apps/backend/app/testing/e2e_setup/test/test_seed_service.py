"""Service tests for ``app/testing/e2e_setup/service.py``.

Verifies that seed helpers produce the expected durable state and that the
new deliberate side-effect — audit rows emitted by public service calls — is
present after seeding.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.audit_log import AuditEntryRow
from app.domain.orgs import MembershipRow, OrgRow
from app.plugins.claude_code import ClaudeCodeSettingsRow
from app.plugins.github import GitHubAppInstallationRow
from app.testing.e2e_setup.service import (
    seed_bootstrap_owner,
    seed_github_install,
    seed_lesson,
)

# ---------------------------------------------------------------------------
# seed_bootstrap_owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_bootstrap_owner_creates_org_and_membership(db_session) -> None:
    """``seed_bootstrap_owner`` produces an org row + owner membership."""
    ids = await seed_bootstrap_owner(
        email="owner@example.com",
        github_id="gh-42",
        org_slug="seed-test-org",
        display_name="Seed Owner",
    )

    assert ids["org_slug"] == "seed-test-org"

    org = (
        await db_session.execute(select(OrgRow).where(OrgRow.slug == "seed-test-org"))
    ).scalar_one_or_none()
    assert org is not None

    membership = (
        await db_session.execute(select(MembershipRow).where(MembershipRow.org_id == org.id))
    ).scalar_one_or_none()
    assert membership is not None
    assert membership.role == "owner"


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_bootstrap_owner_emits_audit_rows(db_session) -> None:
    """``seed_bootstrap_owner`` emits ``org.created`` and ``membership.created`` audit rows."""
    await seed_bootstrap_owner(
        email="auditor@example.com",
        github_id="gh-99",
        org_slug="seed-audit-org",
        display_name="Audit Owner",
    )

    org = (
        await db_session.execute(select(OrgRow).where(OrgRow.slug == "seed-audit-org"))
    ).scalar_one_or_none()
    assert org is not None

    all_audit_rows = (
        (await db_session.execute(select(AuditEntryRow).where(AuditEntryRow.org_id == org.id)))
        .scalars()
        .all()
    )

    kinds = {r.kind for r in all_audit_rows}
    assert "org.created" in kinds
    assert "membership.created" in kinds


# ---------------------------------------------------------------------------
# seed_github_install
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_github_install_creates_expected_rows(db_session) -> None:
    """``seed_github_install`` inserts a GitHub install row + Claude Code settings row."""
    await seed_bootstrap_owner(
        email="gh-owner@example.com",
        github_id="gh-55",
        org_slug="gh-install-seed-org",
        display_name="GH Owner",
    )

    await seed_github_install(
        org_login="acme-test",
        target_org_slug="gh-install-seed-org",
    )

    org = (
        await db_session.execute(select(OrgRow).where(OrgRow.slug == "gh-install-seed-org"))
    ).scalar_one_or_none()
    assert org is not None

    install_row = (
        await db_session.execute(
            select(GitHubAppInstallationRow).where(GitHubAppInstallationRow.org_id == org.id)
        )
    ).scalar_one_or_none()
    assert install_row is not None
    assert install_row.account_login == "acme-test"
    assert install_row.status == "active"

    settings_row = (
        await db_session.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org.id))
    ).scalar_one_or_none()
    assert settings_row is not None
    assert settings_row.encrypted_anthropic_api_key is not None


# ---------------------------------------------------------------------------
# seed_lesson
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_lesson_returns_uuid_and_emits_audit(db_session) -> None:
    """``seed_lesson`` returns a lesson UUID and emits a ``lesson.created`` audit row."""
    from app.domain.lessons import get as get_lesson  # noqa: PLC0415
    from app.testing.e2e_setup.service import DEFAULT_ORG_ID  # noqa: PLC0415

    lesson_id = await seed_lesson(
        repo_external_id="acme/web",
        title="Always validate inputs",
        body="Never trust user-supplied data without validation.",
    )

    assert lesson_id is not None

    lesson = await get_lesson(lesson_id, org_id=DEFAULT_ORG_ID)
    assert lesson.title == "Always validate inputs"
    assert lesson.repo_external_id == "acme/web"

    # ``lessons.create`` emits a ``lesson.created`` audit row.
    audit_rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.entity_id == lesson_id,
                    AuditEntryRow.kind == "lesson.created",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit_rows) == 1
