"""GitHub OAuth App (Login with GitHub).

Drives "Sign in with GitHub" via a yaaos-owned **GitHub OAuth App** — a
distinct GitHub primitive from the GitHub App used for org installs (the
two are not the same thing; GitHub names them confusingly). Credentials
live in `yaaos_github_oauth_client_id` / `_client_secret`; the install
flow's `yaaos_github_app_*` env vars play no part here.

Scopes are configured on the OAuth App registration itself, so we never
pass a `scope` query param.

Implements `core/identity.Provider`.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
import structlog
from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.core.config import get_settings
from app.core.identity import ProviderError, ProviderProfile, register_provider

log = structlog.get_logger("plugins.github.oauth")


class GitHubOAuthProvider:
    """Provider implementation for the platform yaaos GitHub OAuth App.

    Stateless: every call reads settings fresh so test overrides via
    `monkeypatch.setenv` are picked up between requests.
    """

    provider_id = "github"

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        s = get_settings()
        params = {
            "client_id": s.yaaos_github_oauth_client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "allow_signup": "false",
        }
        return f"{s.github_web_base_url}/login/oauth/authorize?{urlencode(params)}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> ProviderProfile:
        s = get_settings()
        token_url = s.yaaos_github_oauth_token_url or f"{s.github_web_base_url}/login/oauth/access_token"
        async with AsyncOAuth2Client(
            client_id=s.yaaos_github_oauth_client_id,
            client_secret=s.yaaos_github_oauth_client_secret.get_secret_value(),
            redirect_uri=redirect_uri,
        ) as client:
            try:
                token = await client.fetch_token(
                    token_url,
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
        async with httpx.AsyncClient(base_url=s.github_api_base_url) as http:
            user_resp = await http.get("/user", headers=headers)
            emails_resp = await http.get("/user/emails", headers=headers)
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
            # GitHub's account-level 2FA gate runs inside the OAuth handshake
            # itself — by the time we get a token the user has already passed
            # that second-factor check. No yaaos TOTP step-up on top.
            mfa_satisfied=True,
            # The GitHub `login` (a.k.a. username/handle). Surfaced so the
            # callback path can write `users.github_username`.
            provider_login=user.get("login"),
        )


def _pick_primary_email(emails: list[dict]) -> dict | None:
    """Returns the entry GitHub flagged as `primary: true`. None if the list
    is empty or no entry is flagged."""
    for entry in emails:
        if entry.get("primary"):
            return entry
    return None


def bootstrap_oauth() -> None:
    """Register the singleton Provider in the in-process registry.

    Skipped when the GitHub OAuth App's client_id / client_secret are unset:
    registering anyway would advertise GitHub login on `/api/auth/providers`
    and then 404 at GitHub with `client_id=`. The LoginPage renders an empty
    list as "no providers configured".
    """
    s = get_settings()
    if not s.yaaos_github_oauth_client_id or not s.yaaos_github_oauth_client_secret.get_secret_value():
        log.info("oauth_github.skipped_unconfigured")
        return
    register_provider(GitHubOAuthProvider())
