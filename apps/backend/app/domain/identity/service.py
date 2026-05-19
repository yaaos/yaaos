"""Service entry-points for `domain/identity`.

This is a skeleton at Phase 1 — concrete login/link/session flows are filled
in by Phases 3-4. Re-exports the public types so callers import from
`app.domain.identity` instead of internal sub-modules.
"""

from __future__ import annotations

from app.domain.identity.types import (
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
    "HardRejectError",
    "LinkChallengeRequiredError",
    "OAuthIdentity",
    "Session",
    "SessionNotFoundError",
    "TotpError",
    "User",
    "UserEmail",
    "UserNotFoundError",
]
