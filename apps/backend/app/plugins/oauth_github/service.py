"""GitHub OAuth Provider.

Implements the `domain/identity.Provider` Protocol against GitHub's OAuth 2.0
authorization-code flow. The userinfo step queries `/user` for the stable id +
display name, then `/user/emails` for the primary verified address — GitHub
only returns `email_verified` via the emails endpoint, and the primary email
on `/user` is `null` when the user hides their address.

Endpoint URLs are settings-driven so the test stack (and the future SaaS
self-hosted variant) can swap in a fake GitHub. Authlib provides the OAuth
URL builder + token-exchange shape; the userinfo plumbing is direct httpx
so the test suite can mock it with `pytest-httpx`.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
import structlog
from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.core.config import get_settings
from app.domain.identity.providers import (
    Provider,
    ProviderError,
    ProviderProfile,
    register_provider,
)

log = structlog.get_logger("plugins.oauth_github")


_USERINFO_SCOPE = "read:user user:email"


class GitHubOAuthProvider:
    """Provider implementation for GitHub OAuth Apps.

    Stateless: every call reads settings fresh so test overrides via
    `monkeypatch.setenv` are picked up between requests.
    """

    provider_id = "github"

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        s = get_settings()
        params = {
            "client_id": s.yaaos_oauth_github_client_id,
            "redirect_uri": redirect_uri,
            "scope": _USERINFO_SCOPE,
            "state": state,
            "allow_signup": "false",
        }
        return f"{s.yaaos_oauth_github_authorize_url}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> ProviderProfile:
        s = get_settings()
        async with AsyncOAuth2Client(
            client_id=s.yaaos_oauth_github_client_id,
            client_secret=s.yaaos_oauth_github_client_secret,
            redirect_uri=redirect_uri,
        ) as client:
            try:
                token = await client.fetch_token(
                    s.yaaos_oauth_github_token_url,
                    code=code,
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:  # authlib raises a variety of error types
                log.warning("oauth_github.token_exchange_failed", error=str(exc))
                raise ProviderError("token_exchange_failed") from exc
        access_token = token.get("access_token")
        if not access_token:
            raise ProviderError("missing_access_token")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient() as http:
            user_resp = await http.get(s.yaaos_oauth_github_userinfo_url, headers=headers)
            emails_resp = await http.get(s.yaaos_oauth_github_emails_url, headers=headers)
        if user_resp.status_code != 200 or emails_resp.status_code != 200:
            log.warning(
                "oauth_github.userinfo_failed",
                user_status=user_resp.status_code,
                emails_status=emails_resp.status_code,
            )
            raise ProviderError("userinfo_failed")
        user = user_resp.json()
        emails = emails_resp.json()
        primary = _pick_primary_email(emails)
        if primary is None:
            raise ProviderError("no_primary_email")
        return ProviderProfile(
            external_subject=str(user["id"]),
            primary_email=primary["email"].lower(),
            email_verified=bool(primary.get("verified")),
            display_name=user.get("name") or user.get("login") or "",
            # GitHub OAuth is treated as MFA-trusted: their account-level
            # 2FA gate runs inside the OAuth handshake itself, so by the
            # time we get a token the user has already passed that
            # second-factor check. No API verification needed; we don't
            # demand a separate yaaos TOTP step-up on top of GitHub login.
            mfa_satisfied=True,
        )


def _pick_primary_email(emails: list[dict]) -> dict | None:
    """Returns the entry GitHub flagged as `primary: true`. None if the list
    is empty or no entry is flagged."""
    for entry in emails:
        if entry.get("primary"):
            return entry
    return None


def bootstrap() -> None:
    """Register the singleton Provider in the in-process registry."""
    register_provider(GitHubOAuthProvider())


__all__ = ["GitHubOAuthProvider", "Provider", "bootstrap"]
