"""Role enum + per-action role-policy map.

`Role` is the shared authorization primitive. It belongs in `core/auth` so
every layer can import it without touching `domain/orgs`.

`_REQUIRED_ROLE` and `required_role_for` are the single source of truth for
the minimum role needed for each `Action`. `core/sessions.dependencies` builds
the `require(action)` dep factory on top of these.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.auth.types import Action


class Role(StrEnum):
    """Three-tier org role; Owner >= Admin >= Builder.

    `OWNER` — full control incl. org deletion, billing, SSO config.
    `ADMIN` — Owner powers minus deleting the org or removing other Owners.
    `BUILDER` — read findings, post replies, trigger reviews, manage own acks.
    """

    OWNER = "owner"
    ADMIN = "admin"
    BUILDER = "builder"

    def covers(self, required: Role) -> bool:
        """True iff this role has at least the privileges of `required`."""
        order = {Role.BUILDER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return order[self] >= order[required]


# Per-action required role minimum. Single source of truth; per-endpoint
# overrides are explicit — write `Depends(require(Action.X))` with the
# action whose row in this map is what you want enforced.
_REQUIRED_ROLE: dict[Action, Role] = {
    Action.IDENTITY_READ_SELF: Role.BUILDER,
    Action.ORG_READ: Role.BUILDER,
    Action.MEMBERS_READ: Role.BUILDER,
    Action.AUDIT_READ: Role.ADMIN,
    Action.USER_UPDATE_SELF: Role.BUILDER,
    Action.MEMBERS_INVITE: Role.ADMIN,
    Action.MEMBERS_REMOVE: Role.ADMIN,
    Action.MEMBERS_CHANGE_ROLE: Role.ADMIN,
    Action.SSO_CONFIGURE: Role.OWNER,
    Action.GITHUB_APP_LINK: Role.OWNER,
    Action.REVIEW_TRIGGER: Role.BUILDER,
    Action.VCS_READ: Role.ADMIN,
    Action.VCS_WRITE: Role.ADMIN,
    Action.CODING_AGENT_READ: Role.ADMIN,
    Action.CODING_AGENT_WRITE: Role.ADMIN,
    Action.BYOK_READ: Role.ADMIN,
    Action.BYOK_WRITE: Role.ADMIN,
    Action.ORG_SETTINGS_WRITE: Role.ADMIN,
    Action.ORG_SETTINGS_READ: Role.ADMIN,
    Action.INTEGRATIONS_READ: Role.ADMIN,
    Action.INTEGRATIONS_WRITE: Role.ADMIN,
    # Builder-grade access for the three routers. Builders are
    # the people who actually work tickets / write lessons / ack findings.
    Action.TICKETS_READ: Role.BUILDER,
    Action.LESSONS_READ: Role.BUILDER,
    Action.LESSONS_WRITE: Role.BUILDER,
    Action.REVIEWER_READ: Role.BUILDER,
    Action.REVIEWER_WRITE: Role.BUILDER,
}


def required_role_for(action: Action) -> Role:
    """Return the minimum Role needed for `action`.

    Raises KeyError if the action isn't in the registry — the test suite
    asserts coverage so this surfaces forgotten-mapping bugs at import time.
    """
    return _REQUIRED_ROLE[action]
