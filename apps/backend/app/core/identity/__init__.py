"""core/identity — users, emails, OAuth identities, sessions, TOTP."""

from app.core.identity import repository, sessions, totp
from app.core.identity.providers import (
    ProviderError,
    ProviderProfile,
    get_provider,
    list_providers,
    register_provider,
)
from app.core.identity.service import (
    EmailAlreadyLinkedError,
    OAuthIdentity,
    Session,
    SessionNotFoundError,
    TotpError,
    User,
    UserEmail,
    UserNotFoundError,
    _delete_user_artifacts_for_tests,
    _set_session_last_seen_for_tests,
    create_email,
    create_oauth_identity,
    create_session,
    create_user,
    login_via_oauth,
)
from app.core.identity.session_dependency import require_session
from app.core.identity.totp import can_be_sso_exempt_owner, has_verified_totp

# NOTE: `identity.user_web` is not imported here to avoid circular imports at
# load time. It appears in `__all__` so tach allows side-effect imports from
# `app/web.py` and tests that do `from app.core.identity import user_web`.

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
    "_delete_user_artifacts_for_tests",
    "_set_session_last_seen_for_tests",
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
    "require_session",
    "sessions",
    "totp",
    "user_web",
]
