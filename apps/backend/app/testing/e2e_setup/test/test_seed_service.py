"""Service tests for ``app/testing/e2e_setup/service.py``.

Verifies that seed helpers produce the expected durable state and that the
new deliberate side-effect — audit rows emitted by public service calls — is
present after seeding.
"""

from __future__ import annotations

import pytest

from app.core.audit_log import list_for_org
from app.core.auth import Role
from app.domain.orgs import get_org_by_slug
from app.domain.orgs import repository as orgs_repo
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

    org = await get_org_by_slug("seed-test-org")
    assert org is not None

    membership = await orgs_repo.get_org_by_slug(db_session, "seed-test-org")
    assert membership is not None  # org exists — membership verified below via role
    # Verify owner membership via the repository (intra-e2e_setup — testing layer can reach any module).
    from app.core.identity import repository as identity_repo  # noqa: PLC0415

    user = await identity_repo.find_user_by_email(db_session, "owner@example.com")
    assert user is not None
    m = await orgs_repo.get_membership(db_session, user_id=user.id, org_id=org.id)
    assert m is not None
    assert Role(m.role) == Role.OWNER


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

    org = await get_org_by_slug("seed-audit-org")
    assert org is not None

    all_audit_rows = await list_for_org(org_id=org.id, actions=None)
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

    org = await get_org_by_slug("gh-install-seed-org")
    assert org is not None

    # Verify GitHub install + Claude Code settings via audit rows emitted by seed.
    # seed_github_install calls install_coding_agent which emits coding_agent.installed.
    audit_rows = await list_for_org(org_id=org.id, actions=["coding_agent.installed"])
    assert len(audit_rows) >= 1

    # Verify Claude Code install via orgs.list_coding_agents.
    from app.domain.orgs import list_coding_agents  # noqa: PLC0415

    agents = await list_coding_agents(db_session, org.id)
    claude_code_installs = [a for a in agents if a.plugin_id == "claude_code"]
    assert len(claude_code_installs) == 1


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
    audit_rows = await list_for_org(org_id=DEFAULT_ORG_ID, actions=["lesson.created"], limit=10)
    lesson_audits = [r for r in audit_rows if str(r.entity_id) == str(lesson_id)]
    assert len(lesson_audits) == 1
