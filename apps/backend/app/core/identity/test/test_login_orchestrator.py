"""End-to-end coverage of `login_via_oauth` — the two-rule policy:

1. (provider, external_subject) hit → existing user, no row creation.
2. Verified-email hit (no identity row) → auto-link, no user creation.
3. No hits → `LoginResult(user=None)`. Caller redirects to
   `/login?reason=not_provisioned`; no rows created. Provisioning is
   invitation-only.
"""

from __future__ import annotations

import pytest

from app.core.identity import repository as repo
from app.core.identity.providers import ProviderProfile
from app.core.identity.service import login_via_oauth


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
    assert result.user is not None
    assert result.user.id == user.id
    assert result.newly_created is False


@pytest.mark.asyncio
async def test_email_match_without_identity_autolinks(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="other", external_subject="9")

    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.user is not None
    assert result.user.id == user.id
    assert result.newly_created is False

    linked = await repo.find_oauth_identity(db_session, provider="github", external_subject="42")
    assert linked is not None
    assert linked.user_id == user.id


@pytest.mark.asyncio
async def test_unknown_user_returns_none_and_creates_no_rows(db_session) -> None:
    """No matching identity, no matching email → `user=None`. The orchestrator
    must NOT create a user / email / identity row in this branch — provisioning
    is invitation-only ."""
    result = await login_via_oauth(db_session, provider_id="github", profile=_profile())
    assert result.user is None
    assert result.newly_created is False

    # Nothing was written.
    identity = await repo.find_oauth_identity(db_session, provider="github", external_subject="42")
    assert identity is None
    user_by_email = await repo.find_user_by_email(db_session, "user@example.com")
    assert user_by_email is None


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
async def test_github_login_writes_username_on_autolink(db_session) -> None:
    """Verified-email auto-link branch also captures provider_login."""
    user = await repo.insert_user(db_session, display_name="Existing")
    await repo.add_email(db_session, user_id=user.id, email="user@example.com", verified=True)
    await login_via_oauth(db_session, provider_id="github", profile=_profile(login="octo-autolink"))
    refreshed = await repo.get_user(db_session, user.id)
    assert refreshed is not None
    assert refreshed.github_username == "octo-autolink"


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
