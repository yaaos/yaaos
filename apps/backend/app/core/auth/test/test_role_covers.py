"""Unit tests for `Role` and role-policy helpers in `core/auth`."""

from __future__ import annotations

import pytest

from app.core.auth import Action, Role, required_role_for

# ---------------------------------------------------------------------------
# Role.covers ordering
# ---------------------------------------------------------------------------


def test_owner_covers_all_roles() -> None:
    assert Role.OWNER.covers(Role.BUILDER)
    assert Role.OWNER.covers(Role.ADMIN)
    assert Role.OWNER.covers(Role.OWNER)


def test_admin_covers_builder_not_owner() -> None:
    assert Role.ADMIN.covers(Role.BUILDER)
    assert Role.ADMIN.covers(Role.ADMIN)
    assert not Role.ADMIN.covers(Role.OWNER)


def test_builder_covers_only_itself() -> None:
    assert Role.BUILDER.covers(Role.BUILDER)
    assert not Role.BUILDER.covers(Role.ADMIN)
    assert not Role.BUILDER.covers(Role.OWNER)


# ---------------------------------------------------------------------------
# required_role_for — every Action has a mapping
# ---------------------------------------------------------------------------


def test_every_action_has_a_required_role() -> None:
    """CI contract: the role-policy map covers every Action member."""
    missing = [a for a in Action if required_role_for(a) not in Role]
    assert not missing, f"Actions missing a role mapping: {missing}"


@pytest.mark.parametrize(
    "action,expected_role",
    [
        (Action.IDENTITY_READ_SELF, Role.BUILDER),
        (Action.ORG_READ, Role.BUILDER),
        (Action.MEMBERS_INVITE, Role.ADMIN),
        (Action.SSO_CONFIGURE, Role.OWNER),
        (Action.BYOK_WRITE, Role.ADMIN),
        (Action.TICKETS_READ, Role.BUILDER),
    ],
)
def test_required_role_spot_checks(action: Action, expected_role: Role) -> None:
    assert required_role_for(action) == expected_role
