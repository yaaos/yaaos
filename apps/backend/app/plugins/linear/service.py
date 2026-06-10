"""Linear hosted-MCP IntegrationProvider.

OAuth URLs + scopes + known tool catalogue + `validate(access_token)` live
here. The provider registers itself into `domain/integrations` at boot so
the service stays free of plugin imports.

`validate` hits `/api/me` on the Linear API — 2xx means the token is good,
non-2xx flips the credential to `last_refresh_status = "failed"`. The
known-tools list is the hardcoded catalogue yaaos defends-in-depth with
the `--allowed-tools` CLI flag + the proxy's allowlist check.
"""

from __future__ import annotations

import httpx
import structlog
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.oauth import ProviderConfig
from app.domain.integrations import register_provider

log = structlog.get_logger("plugins.linear")


_VALIDATE_TIMEOUT_SECONDS = 10.0


def _build_config() -> ProviderConfig:
    s = get_settings()
    return ProviderConfig(
        authorize_url=s.linear_oauth_authorize_url,
        token_url=s.linear_oauth_token_url,
        refresh_url=s.linear_oauth_refresh_url,
        mcp_url=s.linear_mcp_url,
        client_id=s.yaaos_oauth_linear_client_id,
        client_secret=s.yaaos_oauth_linear_client_secret,
        scope_separator=",",
        default_scopes=("read",),
        known_read_tools=(
            "get_issue",
            "search_issues",
            "list_projects",
            "list_cycles",
        ),
        known_write_tools=(
            "update_issue",
            "create_comment",
        ),
        token_auth_style="form",
    )


class LinearProvider:
    provider_id = "linear"

    @property
    def config(self) -> ProviderConfig:
        # Read settings fresh so test overrides via monkeypatch.setenv land
        # without requiring a process restart.
        return _build_config()

    async def validate(self, access_token: SecretStr) -> bool:
        """Minimal upstream call — hits the configured Linear API host. The
        fake-linear test stack provides this endpoint; real Linear exposes
        `/api/me` via its GraphQL surface, but the simple HTTP endpoint
        used by the fake is a stand-in for the production validator."""
        s = get_settings()
        url = f"{s.linear_api_base_url.rstrip('/')}/api/me"
        try:
            async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_SECONDS) as http:
                resp = await http.get(
                    url, headers={"Authorization": f"Bearer {access_token.get_secret_value()}"}
                )
        except httpx.HTTPError as exc:
            log.warning("linear.validate.transport_error", error=str(exc))
            return False
        return 200 <= resp.status_code < 300


_provider = LinearProvider()


def bootstrap() -> None:
    """Register the singleton provider. Skipped when credentials are unset
    (matches the github plugin's pattern: the UI surfaces 'not configured'
    instead of advertising an integration that would 404 upstream)."""
    s = get_settings()
    if not s.yaaos_oauth_linear_client_id or not s.yaaos_oauth_linear_client_secret.get_secret_value():
        log.debug("linear.skipped_unconfigured")
        return
    register_provider(_provider)


def get_provider() -> LinearProvider:
    return _provider
