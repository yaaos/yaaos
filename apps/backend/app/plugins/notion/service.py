"""Notion hosted-MCP IntegrationProvider.

Notion's OAuth has two provider-specific quirks compared to the OAuth-spec
norm:

1. **HTTP Basic on the token endpoint.** `ProviderConfig.token_auth_style =
   "basic"` puts `client_id:client_secret` in an `Authorization: Basic ...`
   header instead of body-encoding them.
2. **Long-lived access tokens that don't expire.** Real Notion doesn't issue
   refresh tokens in the OAuth sense — the access token is good until the
   user revokes the integration. yaaos still walks a refresh path for shape
   parity; the fake's `/v1/oauth/token` returns rotation-shaped refresh
   tokens so the code path is exercised in tests. In production this branch
   rarely fires; when it does, Notion treats refresh-token re-presentation
   as idempotent.

Both quirks are encoded in `ProviderConfig` so they don't leak into
`domain/integrations`.
"""

from __future__ import annotations

import httpx
import structlog
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.oauth import ProviderConfig
from app.domain.integrations import register_provider

log = structlog.get_logger("plugins.notion")


_VALIDATE_TIMEOUT_SECONDS = 10.0


def _build_config() -> ProviderConfig:
    s = get_settings()
    return ProviderConfig(
        authorize_url=s.notion_oauth_authorize_url,
        token_url=s.notion_oauth_token_url,
        refresh_url=s.notion_oauth_refresh_url,
        mcp_url=s.notion_mcp_url,
        client_id=s.yaaos_oauth_notion_client_id,
        client_secret=s.yaaos_oauth_notion_client_secret,
        scope_separator=" ",
        # Notion uses capabilities at the integration level rather than
        # OAuth scopes, so the scope list is empty. yaaos passes an explicit
        # `owner=user` flag via the authorize URL.
        default_scopes=(),
        known_read_tools=(
            "search",
            "query_database",
            "retrieve_page",
            "retrieve_block",
        ),
        known_write_tools=(
            "update_page",
            "create_comment",
        ),
        token_auth_style="basic",
    )


class NotionProvider:
    provider_id = "notion"

    @property
    def config(self) -> ProviderConfig:
        return _build_config()

    async def validate(self, access_token: SecretStr) -> bool:
        """Minimal upstream call — `/v1/users/me`. Real Notion requires the
        `Notion-Version` header; we send a recent value. 2xx is the only
        success signal."""
        s = get_settings()
        url = f"{s.notion_api_base_url.rstrip('/')}/v1/users/me"
        headers = {
            "Authorization": f"Bearer {access_token.get_secret_value()}",
            "Notion-Version": "2022-06-28",
        }
        try:
            async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_SECONDS) as http:
                resp = await http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("notion.validate.transport_error", error=str(exc))
            return False
        return 200 <= resp.status_code < 300


_provider = NotionProvider()


def bootstrap() -> None:
    """Register the singleton provider. Skipped when credentials are unset."""
    s = get_settings()
    if not s.yaaos_oauth_notion_client_id or not s.yaaos_oauth_notion_client_secret.get_secret_value():
        log.info("notion.skipped_unconfigured")
        return
    register_provider(_provider)


def get_provider() -> NotionProvider:
    return _provider
