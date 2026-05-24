"""End-to-end coverage of `login_via_oauth` — the three-rule policy applied to a `ProviderProfile`:

1. (provider, external_subject) hit → existing user, no row creation.
2. Verified-email hit (no identity row) → auto-link, no user creation.
3. No hits → create user + email + identity row. Pending invitation, if
   present for the same email, is accepted as part of creation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.identity import repository as repo
from app.domain.identity.providers import ProviderProfile
from app.domain.identity.service import login_via_oauth
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.models import InvitationRow
from app.domain.orgs.types import Role


def _profile(*, sub="42", email="user@example.com", login: str | None = None) -> ProviderProfile:
    return ProviderProfile(
        external_subject=sub,
        primary_email=email,
        email_verified=True,
        display_name="User",
        provider_login=login,
    )


@pytest.mark.asyncio
async def test_existing_identity_returns_existing_user(db_session) -> None:
    user = await repo.insert_user(db_session, display_name="Old")
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="github", external_subject="42")

    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.user.id == user.id
    assert result.newly_created is False


@pytest.mark.asyncio
async def test_email_match_without_identity_autolinks(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="other", external_subject="9")

    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.user.id == user.id
    assert result.newly_created is False

    linked = await repo.find_oauth_identity(db_session, provider="github", external_subject="42")
    assert linked is not None
    assert linked.user_id == user.id


@pytest.mark.asyncio
async def test_unmatched_email_creates_user(db_session) -> None:
    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.newly_created is True
    created = await repo.get_user(db_session, result.user.id)
    assert created is not None
    emails = await repo.list_emails_for_user(db_session, result.user.id)
    assert any(e.email == "user@example.com" and e.verified_at is not None for e in emails)
    identity = await repo.find_oauth_identity(db_session, provider="github", external_subject="42")
    assert identity is not None
    assert identity.user_id == result.user.id


@pytest.mark.asyncio
async def test_pending_invitation_creates_user_and_membership(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="acme")
    inviter = await repo.insert_user(db_session)
    db_session.add(
        InvitationRow(
            id=uuid4(),
            org_id=org.id,
            email="user@example.com",
            role=Role.BUILDER.value,
            token_hash="x" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            invited_by_user_id=inviter.id,
        )
    )
    await db_session.flush()

    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.newly_created is True

    membership = await orgs_repo.get_membership(db_session, user_id=result.user.id, org_id=org.id)
    assert membership is not None
    assert membership.role == Role.BUILDER.value


@pytest.mark.asyncio
async def test_expired_invitation_still_creates_user_without_membership(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="acme2")
    db_session.add(
        InvitationRow(
            id=uuid4(),
            org_id=org.id,
            email="user@example.com",
            role=Role.BUILDER.value,
            token_hash="y" * 64,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            invited_by_user_id=None,
        )
    )
    await db_session.flush()

    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.newly_created is True
    membership = await orgs_repo.get_membership(db_session, user_id=result.user.id, org_id=org.id)
    assert membership is None


@pytest.mark.asyncio
async def test_github_login_refreshes_github_username(db_session) -> None:
    """On every successful github login, users.github_username is rewritten
    from the profile — handles GitHub renames without manual intervention."""
    user = await repo.insert_user(db_session, display_name="U")
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="github", external_subject="42")

    await login_via_oauth(db_session, provider_id="github", profile=_profile(login="octocat"))
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octocat"

    await login_via_oauth(db_session, provider_id="github", profile=_profile(login="octocat-2"))
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octocat-2"


@pytest.mark.asyncio
async def test_github_login_writes_username_on_autolink_and_signup(db_session) -> None:
    # Auto-link branch.
    user = await repo.insert_user(db_session, display_name="Existing")
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await login_via_oauth(db_session, provider_id="github", profile=_profile(login="octo-autolink"))
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octo-autolink"

    # Fresh-signup branch.
    result = await login_via_oauth(
        db_session,
        provider_id="github",
        profile=_profile(sub="99", email="new@example.com", login="octo-new"),
    )
    refreshed = await repo.get_user(db_session, result.user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octo-new"


@pytest.mark.asyncio
async def test_non_github_provider_does_not_touch_github_username(db_session) -> None:
    user = await repo.insert_user(db_session, display_name="U")
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="okta", external_subject="42")
    # Stamp a known github_username so we can assert it's NOT overwritten.
    await repo.set_user_github_username(db_session, user_id=user.id, github_username="kept")
    await login_via_oauth(db_session, provider_id="okta", profile=_profile())
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "kept"
