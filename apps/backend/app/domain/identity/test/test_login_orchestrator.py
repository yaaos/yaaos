"""End-to-end coverage of `login_via_oauth` — the matching/linking/hard-reject
ladder applied to a `ProviderProfile`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.identity import repository as repo
from app.domain.identity.providers import ProviderProfile
from app.domain.identity.service import (
    HardRejectError,
    LinkChallengeRequiredError,
    complete_oauth_link,
    login_via_oauth,
)
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.models import InvitationRow
from app.domain.orgs.types import Role


def _profile(*, sub="42", email="user@example.com") -> ProviderProfile:
    return ProviderProfile(
        external_subject=sub,
        primary_email=email,
        email_verified=True,
        display_name="User",
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
async def test_email_match_without_identity_raises_link_challenge(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="other", external_subject="9")

    with pytest.raises(LinkChallengeRequiredError):
        await login_via_oauth(db_session, provider_id="github", profile=_profile())


@pytest.mark.asyncio
async def test_unmatched_email_no_invitation_hard_rejects(db_session) -> None:
    with pytest.raises(HardRejectError):
        await login_via_oauth(db_session, provider_id="github", profile=_profile())


@pytest.mark.asyncio
async def test_pending_invitation_creates_user_and_membership(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="acme")
    inviter = await repo.insert_user(db_session)
    db_session.add(
        InvitationRow(
            id=uuid4(),
            org_id=org.id,
            email="user@example.com",
            role=Role.MEMBER.value,
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
    assert membership.role == Role.MEMBER.value


@pytest.mark.asyncio
async def test_github_login_refreshes_github_username(db_session) -> None:
    """On every successful github login, users.github_username is rewritten
    from the profile — handles GitHub renames without manual intervention."""
    user = await repo.insert_user(db_session, display_name="U")
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="github", external_subject="42")
    profile = ProviderProfile(
        external_subject="42",
        primary_email="user@example.com",
        email_verified=True,
        display_name="U",
        provider_login="octocat",
    )
    await login_via_oauth(db_session, provider_id="github", profile=profile)
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octocat"

    # Simulate a GitHub rename: next login carries a new login.
    profile2 = profile.model_copy(update={"provider_login": "octocat-2"})
    await login_via_oauth(db_session, provider_id="github", profile=profile2)
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octocat-2"


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


@pytest.mark.asyncio
async def test_expired_invitation_hard_rejects(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="acme2")
    db_session.add(
        InvitationRow(
            id=uuid4(),
            org_id=org.id,
            email="user@example.com",
            role=Role.MEMBER.value,
            token_hash="y" * 64,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            invited_by_user_id=None,
        )
    )
    await db_session.flush()

    with pytest.raises(HardRejectError):
        await login_via_oauth(db_session, provider_id="github", profile=_profile())


@pytest.mark.asyncio
async def test_complete_oauth_link_attaches_identity(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="x@example.com", verified=True)

    identity = await complete_oauth_link(
        db_session, user_id=user.id, provider_id="github", external_subject="99"
    )
    assert identity.user_id == user.id

    found = await repo.find_oauth_identity(db_session, provider="github", external_subject="99")
    assert found is not None and found.user_id == user.id
