"""Shared types for `domain/orgs` — role enum, exceptions."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Three-enum org role; Owner ≥ Admin ≥ Builder. Fixed for POC.

    M06 renamed the lowest tier from `member` → `builder`. `Owner` is the
    creator-distinct role; `Admin` mutates org-wide settings; `Builder` is
    every other member with full action access but no org-wide mutate rights.
    """

    OWNER = "owner"
    ADMIN = "admin"
    BUILDER = "builder"

    def covers(self, required: Role) -> bool:
        """True iff this role has at least the privileges of `required`."""
        order = {Role.BUILDER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] >= order[required]


class OrgNotFoundError(LookupError):
    """Slug → org lookup failed, or caller has no membership."""


class MembershipNotFoundError(LookupError):
    """No membership for (user_id, org_id)."""


class InsufficientRoleError(PermissionError):
    """Membership exists but role doesn't cover the required action."""


class InvitationError(ValueError):
    """Invitation token expired, already used, or malformed."""
