"""core/identity — users, emails, OAuth identities, sessions, TOTP."""

# Rename mapping for sessions functions (disambiguated from service-layer names):
#   sessions.create             → mint_session
#   sessions.lookup             → lookup_session
#   sessions.touch              → touch_session
#   sessions.revoke             → revoke_session
#   sessions.revoke_all_for_user → revoke_all_sessions_for_user
#   sessions.rotate             → rotate_session
#   sessions.mark_sso_satisfied → mark_sso_satisfied  (unique, no rename)
#   sessions.is_sso_satisfied   → is_sso_satisfied    (unique, no rename)
#   sessions.cleanup_expired    → cleanup_expired_sessions
# Rename mapping for totp functions:
#   totp.enroll                 → enroll_totp
#   totp.verify                 → verify_totp
#   (has_verified_totp and can_be_sso_exempt_owner already flat-exported below)

from app.core.identity.providers import (
    ProviderError,
    ProviderProfile,
    get_provider,
    list_providers,
    register_provider,
)
from app.core.identity.repository import (
    add_email,
    add_oauth_identity,
    count_verified_emails,
    delete_email,
    find_oauth_identity,
    find_user_by_email,
    get_session_by_hash,
    get_totp_secret,
    get_user,
    hash_token,
    insert_session,
    insert_user,
    list_emails_for_user,
    set_user_display_name,
    set_user_github_username,
    upsert_totp_secret,
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
    create_email,
    create_oauth_identity,
    create_session,
    create_user,
    login_via_oauth,
)
from app.core.identity.session_dependency import require_session
from app.core.identity.sessions import (
    CreatedSession,
    is_sso_satisfied,
    mark_sso_satisfied,
)
from app.core.identity.sessions import (
    cleanup_expired as cleanup_expired_sessions,
)
from app.core.identity.sessions import (
    create as mint_session,
)
from app.core.identity.sessions import (
    lookup as lookup_session,
)
from app.core.identity.sessions import (
    revoke as revoke_session,
)
from app.core.identity.sessions import (
    revoke_all_for_user as revoke_all_sessions_for_user,
)
from app.core.identity.sessions import (
    rotate as rotate_session,
)
from app.core.identity.sessions import (
    touch as touch_session,
)
from app.core.identity.totp import (
    can_be_sso_exempt_owner,
    has_verified_totp,
)
from app.core.identity.totp import (
    enroll as enroll_totp,
)
from app.core.identity.totp import (
    verify as verify_totp,
)

# NOTE: `identity.user_web` is not imported here to avoid circular imports at
# load time. It appears in `__all__` so tach allows side-effect imports from
# `app/web.py` and tests that do `from app.core.identity import user_web`.

__all__ = [
    "CreatedSession",
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
    "add_email",
    "add_oauth_identity",
    "can_be_sso_exempt_owner",
    "cleanup_expired_sessions",
    "count_verified_emails",
    "create_email",
    "create_oauth_identity",
    "create_session",
    "create_user",
    "delete_email",
    "enroll_totp",
    "find_oauth_identity",
    "find_user_by_email",
    "get_provider",
    "get_session_by_hash",
    "get_totp_secret",
    "get_user",
    "has_verified_totp",
    "hash_token",
    "insert_session",
    "insert_user",
    "is_sso_satisfied",
    "list_emails_for_user",
    "list_providers",
    "login_via_oauth",
    "lookup_session",
    "mark_sso_satisfied",
    "mint_session",
    "register_provider",
    "require_session",
    "revoke_all_sessions_for_user",
    "revoke_session",
    "rotate_session",
    "set_user_display_name",
    "set_user_github_username",
    "touch_session",
    "upsert_totp_secret",
    "user_web",
    "verify_totp",
]
