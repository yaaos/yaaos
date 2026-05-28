"""GitHub OAuth Provider integration tests.

`pytest-httpx` mocks GitHub's token + userinfo + emails endpoints so the real
`exchange_code` path runs end-to-end without network. The provider drives the
platform yaaos GitHub App's user-to-server OAuth flow.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import get_settings
from app.domain.identity import ProviderError
from app.plugins.github.oauth import GitHubOAuthProvider


def test_authorization_url_contains_required_params(monkeypatch) -> None:
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "test-client")
    get_settings.cache_clear()
    p = GitHubOAuthProvider()
    url = p.authorization_url(state="abc.def", redirect_uri="http://test/api/auth/callback/github")
    parts = urlparse(url)
    q = parse_qs(parts.query)
    assert parts.netloc == "github.com"
    assert q["client_id"] == ["test-client"]
    assert q["state"] == ["abc.def"]
    assert q["redirect_uri"] == ["http://test/api/auth/callback/github"]
    # GitHub App user-auth scopes are configured on the App registration, not
    # requested per-call. The provider must NOT pass a `scope` param.
    assert "scope" not in q


@pytest.mark.asyncio
async def test_exchange_code_happy_path(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()

    httpx_mock.add_response(
        url="https://github.com/login/oauth/access_token",
        method="POST",
        json={"access_token": "gh-token", "token_type": "bearer", "scope": "read:user"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/user",
        method="GET",
        json={"id": 42, "login": "octocat", "name": "Octo Cat"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/user/emails",
        method="GET",
        json=[
            {"email": "secondary@example.com", "primary": False, "verified": True},
            {"email": "OCTO@example.com", "primary": True, "verified": True},
        ],
    )

    p = GitHubOAuthProvider()
    profile = await p.exchange_code(code="abc", redirect_uri="http://test/cb")
    assert profile.external_subject == "42"
    assert profile.primary_email == "octo@example.com"
    assert profile.email_verified is True
    assert profile.display_name == "Octo Cat"
    # `provider_login` surfaces GitHub `login` for users.github_username.
    assert profile.provider_login == "octocat"


@pytest.mark.asyncio
async def test_exchange_code_unverified_primary(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()

    httpx_mock.add_response(
        url="https://github.com/login/oauth/access_token",
        method="POST",
        json={"access_token": "gh-token", "token_type": "bearer"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/user",
        method="GET",
        json={"id": 7, "login": "x", "name": "X"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/user/emails",
        method="GET",
        json=[{"email": "x@example.com", "primary": True, "verified": False}],
    )

    profile = await GitHubOAuthProvider().exchange_code(code="abc", redirect_uri="http://test/cb")
    assert profile.email_verified is False  # caller (the /callback handler) rejects this


@pytest.mark.asyncio
async def test_exchange_code_userinfo_failure_raises(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()

    httpx_mock.add_response(
        url="https://github.com/login/oauth/access_token",
        method="POST",
        json={"access_token": "gh-token", "token_type": "bearer"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/user",
        method="GET",
        status_code=401,
        json={"message": "Bad credentials"},
    )
    httpx_mock.add_response(
        url="https://api.github.com/user/emails",
        method="GET",
        status_code=401,
        json={"message": "Bad credentials"},
    )

    with pytest.raises(ProviderError):
        await GitHubOAuthProvider().exchange_code(code="abc", redirect_uri="http://test/cb")


@pytest.mark.asyncio
async def test_exchange_code_token_failure_raises(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()

    httpx_mock.add_response(
        url="https://github.com/login/oauth/access_token",
        method="POST",
        status_code=401,
        json={"error": "bad_verification_code"},
    )

    with pytest.raises(ProviderError):
        await GitHubOAuthProvider().exchange_code(code="abc", redirect_uri="http://test/cb")
