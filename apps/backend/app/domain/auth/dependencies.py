"""FastAPI dependencies — `require(action)` and `public_route`.

Every M02-protected route declares exactly one of these. The middleware's
post-response guard 500s any route under the M02 protected prefixes that
left `route_security_resolved` unset.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.core.auth.context import (
    actor_id_var,
    actor_kind_var,
    org_id_var,
    route_security_resolved,
    user_id_var,
)
from app.core.auth.types import Action
from app.core.database import session as db_session
from app.core.primitives import Actor, ActorKind
from app.domain.identity import repository as identity_repo
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.service import Membership
from app.domain.orgs.types import Role

# Per-action required role minimum. Single source of truth; per-endpoint
# overrides are explicit — write `Depends(require(Action.X))` with the
# action whose row in this map is what you want enforced.
_REQUIRED_ROLE: dict[Action, Role] = {
    Action.IDENTITY_READ_SELF: Role.MEMBER,
    Action.ORG_READ: Role.MEMBER,
    Action.MEMBERS_READ: Role.MEMBER,
    Action.AUDIT_READ: Role.ADMIN,
    Action.ACCOUNT_UPDATE_SELF: Role.MEMBER,
    Action.MEMBERS_INVITE: Role.ADMIN,
    Action.MEMBERS_REMOVE: Role.ADMIN,
    Action.MEMBERS_CHANGE_ROLE: Role.ADMIN,
    Action.SSO_CONFIGURE: Role.OWNER,
    Action.GITHUB_APP_LINK: Role.OWNER,
    Action.REVIEW_TRIGGER: Role.MEMBER,
}


def required_role_for(action: Action) -> Role:
    """Lookup the minimum Role needed for `action`. Raises KeyError if the
    action isn't in the registry — the test suite asserts coverage so this
    surfaces forgotten-mapping bugs at import time."""
    return _REQUIRED_ROLE[action]


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


async def _current_session_user_id(
    yaaos_session: Annotated[str | None, Cookie()] = None,
):
    """Resolve the session cookie → user_id. Sets the `user_id` contextvar.

    Returns None when the cookie is absent or the session is expired/unknown.
    Concrete session expiry checks ship in Phase 3; here we just attempt the
    lookup so the contextvar is populated when a valid session exists.
    """
    if not yaaos_session:
        return None
    token_hash = identity_repo.hash_token(yaaos_session)
    async with db_session() as s:
        row = await identity_repo.get_session_by_hash(s, token_hash)
    if row is None or row.user_id is None:
        return None
    from datetime import UTC, datetime  # noqa: PLC0415

    if row.expires_at < datetime.now(UTC):
        return None
    user_id_var.set(row.user_id)
    return row.user_id


def require(action: Action) -> Callable[..., None]:
    """Dependency factory. Resolves `X-Org-Slug` → org → membership → role check.

    On success, sets `org_id`, `user_id`, `actor_kind`, `actor_id` contextvars
    + `route_security_resolved = "membership"`. The middleware reads these
    when shaping the response and validating the post-response guard.

    Error shape:
      - No session → 401 unauthenticated.
      - No X-Org-Slug header → middleware already 400'd; this dep won't run.
      - Org not found OR caller has no membership → 404 (don't leak existence).
      - Role insufficient → 403.
    """

    required = required_role_for(action)

    async def _dep(
        request: Request,
        x_org_slug: Annotated[str | None, Header(alias="X-Org-Slug")] = None,
        user_id=Depends(_current_session_user_id),
    ) -> Membership:
        if user_id is None:
            raise _err(401, "unauthenticated")
        if not x_org_slug:
            # Middleware should have caught this, but defend in depth.
            raise _err(400, "missing_org_slug")
        async with db_session() as s:
            org_row = await orgs_repo.get_org_by_slug(s, x_org_slug)
            if org_row is None:
                raise _err(404, "org_not_found")
            membership_row = await orgs_repo.get_membership(s, user_id=user_id, org_id=org_row.id)
        if membership_row is None:
            # Mask existence — same shape as "org not found".
            raise _err(404, "org_not_found")
        role = Role(membership_row.role)
        if not role.covers(required):
            raise _err(403, "insufficient_role")
        org_id_var.set(org_row.id)
        actor_kind_var.set(ActorKind.USER)
        actor_id_var.set(user_id)
        route_security_resolved.set("membership")
        return Membership.from_row(membership_row)

    return _dep


async def public_route(request: Request) -> None:
    """Dependency for routes that intentionally have no auth requirement.

    Sets `route_security_resolved = "public"` so the middleware's post-response
    guard recognizes the declaration. Reserved for login/health/etc. — using
    this on a route that should require a role is the bug we are trying to
    catch.
    """
    route_security_resolved.set("public")


def current_actor() -> Actor:
    """Helper for handlers that need to write an audit entry. Reads the
    contextvars `require(...)` set. Raises if called before `require`."""
    user_id = user_id_var.get()
    if user_id is None:
        raise RuntimeError("current_actor() called without an authenticated session")
    return Actor.user(user_id=user_id)
