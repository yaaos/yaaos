"""Shared types for `domain/orgs` — role enum, exceptions."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Three-enum org role; Owner ≥ Admin ≥ Member. Fixed for POC."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

    def covers(self, required: Role) -> bool:
        """True iff this role has at least the privileges of `required`."""
        order = {Role.MEMBER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] >= order[required]


class OrgNotFoundError(LookupError):
    """Slug → org lookup failed, or caller has no membership."""


class MembershipNotFoundError(LookupError):
    """No membership for (user_id, org_id)."""


class InsufficientRoleError(PermissionError):
    """Membership exists but role doesn't cover the required action."""


class InvitationError(ValueError):
    """Invitation token expired, already used, or malformed."""
