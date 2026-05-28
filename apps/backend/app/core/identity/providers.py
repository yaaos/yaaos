"""OAuth `Provider` Protocol + in-process registry.

Each provider plugin (`plugins/github`, `plugins/oauth_test`) implements
this Protocol and registers itself at import time via `register_provider`.
The login endpoint (`/api/auth/login`) and callback endpoint
(`/api/auth/callback/{provider}`) consume providers by id through
`get_provider` and `list_providers`.

A `ProviderProfile` is the normalized payload a provider returns from
`exchange_code`. The login orchestrator (`core/identity.service.login_via_oauth`)
applies the matching / linking / hard-reject rules against the profile —
provider plugins are responsible only for talking to the IdP and producing
the normalized shape.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ProviderProfile(BaseModel):
    """Normalized identity payload after a successful OAuth callback.

    `email_verified` mirrors GitHub's `verified: true` on the primary
    email. Unverified addresses never enter the login flow — the
    callback handler rejects them before consulting the registry.

    `mfa_satisfied` reports whether the provider itself enforced a
    second factor for this signin. GitHub is treated as MFA-trusted
    (their account-level 2FA gate runs inside the OAuth handshake);
    generic OIDC providers parse `amr`/`acr` from the id_token and
    set this based on the claim. yaaos's TOTP step-up only fires when
    the user has a verified TOTP secret AND the provider returned
    `mfa_satisfied=False`.
    """

    external_subject: str
    primary_email: str
    email_verified: bool
    display_name: str
    mfa_satisfied: bool = False
    # Provider-specific login/handle (e.g. GitHub `login`). Surfaced so the
    # `oauth_github` callback path can persist it as `users.github_username`.
    # Other providers may leave it `None`.
    provider_login: str | None = None


class ProviderError(RuntimeError):
    """Raised by Provider implementations when the IdP exchange fails
    (token endpoint returned non-2xx, userinfo endpoint refused, primary
    email could not be resolved)."""


@runtime_checkable
class Provider(Protocol):
    """OAuth-style identity provider."""

    provider_id: str

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """URL the user agent should be 302'd to."""
        ...

    async def exchange_code(self, *, code: str, redirect_uri: str) -> ProviderProfile:
        """Exchange `code` for a normalized profile. Raises `ProviderError`
        on token-exchange or userinfo failure."""
        ...


_REGISTRY: dict[str, Provider] = {}


def register_provider(provider: Provider) -> None:
    """Idempotent: re-registering the same id overwrites — plugin bootstraps
    may run more than once across test/dev hot-reload cycles."""
    _REGISTRY[provider.provider_id] = provider


def get_provider(provider_id: str) -> Provider | None:
    return _REGISTRY.get(provider_id)


def list_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """Test hook — not used in production code."""
    _REGISTRY.clear()
