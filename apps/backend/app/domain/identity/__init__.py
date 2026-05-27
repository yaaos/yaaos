"""domain/identity — users, emails, OAuth identities, sessions, TOTP."""

from app.domain.identity import repository, sessions, totp
from app.domain.identity.providers import (
    ProviderError,
    ProviderProfile,
    get_provider,
    list_providers,
    register_provider,
)
from app.domain.identity.service import (
    EmailAlreadyLinkedError,
    OAuthIdentity,
    Session,
    SessionNotFoundError,
    TotpError,
    User,
    UserEmail,
    UserNotFoundError,
    create_email,
    create_oauth_identity,
    create_session,
    create_user,
    login_via_oauth,
)
from app.domain.identity.totp import can_be_sso_exempt_owner, has_verified_totp

__all__ = [
    "EmailAlreadyLinkedError",
    "OAuthIdentity",
    "ProviderError",
    "ProviderProfile",
    "Session",
    "SessionNotFoundError",
    "TotpError",
    "User",
    "UserEmail",
    "UserNotFoundError",
    "can_be_sso_exempt_owner",
    "create_email",
    "create_oauth_identity",
    "create_session",
    "create_user",
    "get_provider",
    "has_verified_totp",
    "list_providers",
    "login_via_oauth",
    "register_provider",
    "repository",
    "sessions",
    "totp",
]
