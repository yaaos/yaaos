"""Test-only OAuth Provider.

Loads only when `app_mode == "test"`. Backend integration tests + Playwright
E2E call `set_next_profile(...)` to stage the identity the next `exchange_code`
call will return, then drive the real `/api/auth/callback/test` route. No
HTTP traffic, no token exchange — the goal is to exercise the login
orchestration end-to-end without depending on GitHub.

Also registers a device-code `UserOAuthApp` (`provider_id="test"`) against
`core/oauth`'s user-connection subsystem, pointed at the `fake-oauth-provider`
peer in the test stack. This keeps the generic device-code flow (start,
poll, grant, connect/disconnect) e2e-tested even though no production plugin
registers one today.
"""

from __future__ import annotations

import base64
import json

from app.core.config import get_settings
from app.core.identity import ProviderProfile, register_provider
from app.core.oauth import Tokens, UserOAuthApp, register_user_oauth_app

assert get_settings().is_test, "plugins.oauth_test refuses to load outside APP_MODE=test"


_NEXT_PROFILE: ProviderProfile | None = None


def set_next_profile(profile: ProviderProfile | None) -> None:
    """Stage the profile that the next call to `exchange_code` will return.

    Pass `None` to clear. Tests set this in setup; the callback handler
    consumes it; tests assert the rest.
    """
    global _NEXT_PROFILE
    _NEXT_PROFILE = profile


class TestOAuthProvider:
    provider_id = "test"

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """The test provider's "authorize" loop is `redirect_uri?code=test-code&state=...`.

        Playwright clicks "Sign in (test)" → the SPA hits this URL → we
        echo back a redirect with a canned `code` so the callback flow runs
        unchanged. Integration tests typically skip this hop and POST/GET
        the callback directly.
        """
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}code=test-code&state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> ProviderProfile:
        if _NEXT_PROFILE is None:
            raise RuntimeError("oauth_test: no profile staged; call set_next_profile first")
        return _NEXT_PROFILE


def _test_provider_account_id(tokens: Tokens) -> str | None:
    """Extract the account id from the id_token JWT `sub` claim.

    No signature verification — this is a test-only fake provider; the JWT
    is minted by `fake-oauth-provider`, not a real identity provider.
    """
    if tokens.id_token is None:
        return None
    try:
        raw = tokens.id_token.get_secret_value()
        parts = raw.split(".")
        if len(parts) != 3:
            return None
        padding = 4 - len(parts[1]) % 4
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (padding % 4))
        data = json.loads(payload)
        return data.get("sub")
    except Exception:
        return None


def bootstrap() -> None:
    register_provider(TestOAuthProvider())

    settings = get_settings()
    base_url = settings.yaaos_oauth_test_provider_base_url.rstrip("/")

    try:
        register_user_oauth_app(
            UserOAuthApp(
                provider_id="test",
                display_name="Test Provider",
                connect_hint="Authorize yaaos in the Test Provider settings.",
                flow="device_code",
                device_authorize_url=f"{base_url}/device/code",
                token_url=f"{base_url}/token",
                client_id="test-client",
                client_secret=None,  # Public client
                default_scopes=("openid",),
                token_auth_style="form",
                expiry_source="jwt_exp",
                capture_id_token=True,
                account_id_extractor=_test_provider_account_id,
                relevance_fn=None,  # Always visible on the Connections page.
            )
        )
    except ValueError:
        # Already registered (bootstrap called twice in tests) — safe to skip.
        pass


__all__ = ["TestOAuthProvider", "bootstrap", "set_next_profile"]
