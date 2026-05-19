"""domain/identity — users, emails, OAuth identities, sessions, TOTP."""

from app.domain.identity import sessions, web
from app.domain.identity.models import (
    GithubInstallationRow,
    OAuthIdentityRow,
    SessionRow,
    UserEmailRow,
    UserRow,
    UserTotpSecretRow,
)
from app.domain.identity.service import (
    EmailAlreadyLinkedError,
    HardRejectError,
    LinkChallengeRequiredError,
    OAuthIdentity,
    Session,
    SessionNotFoundError,
    TotpError,
    User,
    UserEmail,
    UserNotFoundError,
)

__all__ = [
    "EmailAlreadyLinkedError",
    "GithubInstallationRow",
    "HardRejectError",
    "LinkChallengeRequiredError",
    "OAuthIdentity",
    "OAuthIdentityRow",
    "Session",
    "SessionNotFoundError",
    "SessionRow",
    "TotpError",
    "User",
    "UserEmail",
    "UserEmailRow",
    "UserNotFoundError",
    "UserRow",
    "UserTotpSecretRow",
    "sessions",
    "web",
]
