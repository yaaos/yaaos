"""Test-only OAuth Provider.

Loads only when `yaaos_env == "test"`. Backend integration tests + Playwright
E2E call `set_next_profile(...)` to stage the identity the next `exchange_code`
call will return, then drive the real `/api/auth/callback/test` route. No
HTTP traffic, no token exchange — the goal is to exercise the login
orchestration end-to-end without depending on GitHub.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.identity import ProviderProfile, register_provider

assert get_settings().yaaos_env == "test", "plugins.oauth_test refuses to load outside YAAOS_ENV=test"


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


def bootstrap() -> None:
    register_provider(TestOAuthProvider())


__all__ = ["TestOAuthProvider", "bootstrap", "set_next_profile"]
