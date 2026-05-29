"""Shared types for `domain/orgs` — exceptions.

`Role` lives in `core.auth` — import it from there.
"""

from __future__ import annotations


class OrgNotFoundError(LookupError):
    """Slug → org lookup failed, or caller has no membership."""


class MembershipNotFoundError(LookupError):
    """No membership for (user_id, org_id)."""


class InsufficientRoleError(PermissionError):
    """Membership exists but role doesn't cover the required action."""


class InvitationError(ValueError):
    """Invitation token expired, already used, or malformed."""
