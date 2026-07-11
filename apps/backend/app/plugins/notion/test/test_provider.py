"""NotionProvider config + validate against a stubbed Notion API."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.plugins.notion.service import NotionProvider


def test_provider_id() -> None:
    assert NotionProvider.provider_id == "notion"


def test_config_uses_settings(monkeypatch) -> None:
    monkeypatch.setenv("YAAOS_OAUTH_NOTION_CLIENT_ID", "notion-id")
    monkeypatch.setenv("YAAOS_OAUTH_NOTION_CLIENT_SECRET", "notion-secret")
    monkeypatch.setenv("NOTION_OAUTH_AUTHORIZE_URL", "http://example/authorize")
    get_settings.cache_clear()

    cfg = NotionProvider().config
    assert cfg.client_id == "notion-id"
    assert cfg.client_secret.get_secret_value() == "notion-secret"
    assert cfg.authorize_url == "http://example/authorize"
    # Notion-specific quirks live in the provider config, not in
    # domain/integrations.
    assert cfg.token_auth_style == "basic"


def test_config_lists_known_tools() -> None:
    cfg = NotionProvider().config
    assert "search" in cfg.known_read_tools
    assert "update_page" in cfg.known_write_tools


@pytest.mark.asyncio
async def test_validate_returns_true_on_2xx(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv("NOTION_API_BASE_URL", "http://stub.notion.test")
    get_settings.cache_clear()
    httpx_mock.add_response(
        method="GET",
        url="http://stub.notion.test/v1/users/me",
        json={"object": "user", "id": "u1"},
    )
    assert await NotionProvider().validate(SecretStr("access-1")) is True


@pytest.mark.asyncio
async def test_validate_returns_false_on_4xx(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv("NOTION_API_BASE_URL", "http://stub.notion.test")
    get_settings.cache_clear()
    httpx_mock.add_response(
        method="GET",
        url="http://stub.notion.test/v1/users/me",
        status_code=401,
        json={"error": "unauthenticated"},
    )
    assert await NotionProvider().validate(SecretStr("bad-token")) is False


@pytest.mark.asyncio
async def test_validate_returns_false_on_transport_error(monkeypatch) -> None:
    """Network failures are treated as 'key invalid', matching the
    same-shape error UX from the api_keys validator."""
    monkeypatch.setenv("NOTION_API_BASE_URL", "http://no-such-host.test")
    get_settings.cache_clear()
    assert await NotionProvider().validate(SecretStr("access-1")) is False
