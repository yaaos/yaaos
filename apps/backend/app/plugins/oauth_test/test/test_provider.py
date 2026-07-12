"""Test-stub provider sanity checks."""

from __future__ import annotations

import pytest

from app.core.identity import ProviderProfile, get_provider
from app.core.oauth import get_user_oauth_app
from app.plugins.oauth_test import set_next_profile


def test_test_provider_is_registered() -> None:
    p = get_provider("test")
    assert p is not None
    assert p.provider_id == "test"


def test_authorization_url_echoes_state() -> None:
    p = get_provider("test")
    url = p.authorization_url(state="s", redirect_uri="http://test/cb")
    assert "code=test-code" in url
    assert "state=s" in url


@pytest.mark.asyncio
async def test_exchange_code_returns_staged_profile() -> None:
    staged = ProviderProfile(
        external_subject="t-1",
        primary_email="a@example.com",
        email_verified=True,
        display_name="A",
    )
    set_next_profile(staged)
    p = get_provider("test")
    out = await p.exchange_code(code="test-code", redirect_uri="http://test/cb")
    set_next_profile(None)
    assert out == staged


@pytest.mark.asyncio
async def test_exchange_code_without_staged_profile_raises() -> None:
    set_next_profile(None)
    p = get_provider("test")
    with pytest.raises(RuntimeError):
        await p.exchange_code(code="x", redirect_uri="http://test/cb")


def test_device_code_user_oauth_app_is_registered() -> None:
    """oauth_test registers a device-code UserOAuthApp under provider_id='test'
    so the generic device-code connect/disconnect flow stays e2e-tested."""
    app = get_user_oauth_app("test")
    assert app.provider_id == "test"
    assert app.flow == "device_code"
    assert app.client_secret is None
    assert app.relevance_fn is None
    assert app.device_authorize_url.endswith("/device/code")
    assert app.token_url.endswith("/token")
