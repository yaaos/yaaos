"""FastAPI dependencies — `require(action)` and `public_route`.

Every ORG_SCOPED route declares `require(action)`. Routes outside the
ORG_SCOPED category use `public_route` (PUBLIC) or rely on the middleware
classifying them as USER_SCOPED. The middleware's post-response guard 500s
any `/api/*` route that left `route_security_resolved` unset.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.core.audit_log import Actor, ActorKind
from app.core.auth import (
    Action,
    AuthFailure,
    Role,
    actor_id_var,
    actor_kind_var,
    org_id_var,
    org_slug_in_query_allowed,
    required_role_for,
    route_security_resolved,
    user_id_var,
)
from app.core.database import session as db_session
from app.core.identity import repository as identity_repo
from app.core.tenancy import AuthOrg, resolve_auth_org


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


async def _current_session_user_id(
    yaaos_session: Annotated[str | None, Cookie()] = None,
):
    """Resolve the session cookie → user_id. Sets the `user_id` contextvar.

    Returns None when the cookie is absent or the session is expired/unknown.
    Attempts the lookup so the contextvar is populated when a valid session
    exists.
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
    """Dependency factory. Resolves `X-Yaaos-Org-Slug` → org → membership → role check.

    On success, sets `org_id`, `user_id`, `actor_kind`, `actor_id` contextvars
    + `route_security_resolved = "org_scoped"`. The middleware reads these
    when shaping the response and validating the post-response guard.

    Error shape:
      - No session → 401 unauthenticated.
      - No X-Yaaos-Org-Slug header → middleware already 400'd; this dep won't run.
      - Org not found OR caller has no membership → 404 (don't leak existence).
      - Role insufficient → 403.
    """

    required = required_role_for(action)

    async def _dep(
        request: Request,
        x_org_slug: Annotated[str | None, Header(alias="X-Yaaos-Org-Slug")] = None,
        user_id=Depends(_current_session_user_id),
    ) -> AuthOrg:
        if user_id is None:
            # Raise the AuthFailure subclass so the registered handler
            # clears the stale session + csrf cookies on the way out.
            # Browser's next request starts clean → no cascading 401 loop.
            raise AuthFailure("unauthenticated")
        # SSE routes accept the slug via the `org` query param because the
        # browser EventSource API cannot set the X-Yaaos-Org-Slug header. Same
        # membership check applies (see `org_slug_in_query_allowed`).
        slug = x_org_slug
        if not slug and org_slug_in_query_allowed(request.url.path):
            slug = request.query_params.get("org")
        if not slug:
            # Middleware should have caught this, but defend in depth.
            raise _err(400, "missing_org_slug")

        async with db_session() as s:
            auth_org = await resolve_auth_org(s, user_id=user_id, slug=slug)

        if auth_org is None:
            # Mask existence — same shape whether org is absent or user has no membership.
            raise _err(404, "org_not_found")

        role = auth_org.role
        if not role.covers(required):
            raise _err(403, "insufficient_role")

        # Idle-session timeout — per-org override, falls back to the global
        # constant. Session is treated as expired when it hasn't been touched
        # within the effective window. Honored by every org-scoped endpoint.
        from datetime import UTC as _UTC  # noqa: PLC0415
        from datetime import datetime as _datetime  # noqa: PLC0415
        from datetime import timedelta as _timedelta  # noqa: PLC0415

        from app.core.auth import SESSION_IDLE_TIMEOUT  # noqa: PLC0415

        token = request.cookies.get("yaaos_session")
        if token:
            token_hash = identity_repo.hash_token(token)
            async with db_session() as s:
                sess_row = await identity_repo.get_session_by_hash(s, token_hash)
            if sess_row is not None and sess_row.last_seen_at is not None:
                minutes = auth_org.session_timeout_override
                idle = _timedelta(minutes=minutes) if minutes else SESSION_IDLE_TIMEOUT
                if sess_row.last_seen_at + idle < _datetime.now(_UTC):
                    # Audit row mirrors the hard-expiry pattern in
                    # `core/identity/scheduler._purge_expired_sessions`
                    # so the timeline has a "why did my session die"
                    # entry for the idle case too.
                    from pydantic import BaseModel as _BaseModel  # noqa: PLC0415

                    from app.core.audit_log import Actor as _Actor  # noqa: PLC0415
                    from app.core.audit_log import audit as _audit  # noqa: PLC0415

                    class _IdlePayload(_BaseModel):
                        kind: str = "idle_timeout"

                    async with db_session() as s:
                        await _audit(
                            "user",
                            user_id,
                            "logout",
                            _IdlePayload(),
                            _Actor.user(user_id=user_id),
                            org_id=auth_org.org_id,
                            session=s,
                        )
                        await s.commit()
                    raise AuthFailure("session_idle_expired")

        # SSO satisfaction: if the org has SSO enabled, the session must
        # have `sso_satisfied_for_org_id == org_id` within the 8h TTL.
        # Break-glass: the exempt Owner bypasses this AND must have a
        # verified TOTP secret.
        from app.core.identity import has_verified_totp  # noqa: PLC0415
        from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415

        if auth_org.sso_enabled:
            session_token = request.cookies.get("yaaos_session")
            sso_ok = False
            if session_token:
                async with db_session() as s:
                    sess = await session_lifecycle.lookup(s, session_token)
                if sess is not None and session_lifecycle.is_sso_satisfied(sess, org_id=auth_org.org_id):
                    sso_ok = True
            if not sso_ok:
                is_exempt = auth_org.sso_exempt_owner_user_id == user_id and role == Role.OWNER
                if is_exempt:
                    async with db_session() as s:
                        if not await has_verified_totp(s, user_id):
                            raise _err(403, "sso_required")
                        # Break-glass: exempt Owner bypassing SSO. Emit a
                        # distinct audit row so abuse is visible.
                        from pydantic import BaseModel  # noqa: PLC0415

                        from app.core.audit_log import Actor, audit  # noqa: PLC0415

                        class _BreakGlassPayload(BaseModel):
                            break_glass: bool = True
                            path: str

                        await audit(
                            "user",
                            user_id,
                            "break_glass_exempt_owner",
                            _BreakGlassPayload(path=request.url.path),
                            Actor.user(user_id=user_id),
                            org_id=auth_org.org_id,
                            session=s,
                        )
                        await s.commit()
                else:
                    raise _err(403, "sso_required")

        org_id_var.set(auth_org.org_id)
        actor_kind_var.set(ActorKind.USER)
        actor_id_var.set(user_id)
        route_security_resolved.set("org_scoped")
        # Bind structlog so log lines + the inner handler carry the identity.
        # Middleware unbinds at request end.
        from app.core.auth import bind_request_structlog_vars  # noqa: PLC0415

        bind_request_structlog_vars()
        # Best-effort: touch the session row so `last_seen_at` reflects
        # actual usage. Single-write per authenticated request; cheap.
        session_cookie = request.cookies.get("yaaos_session")
        if session_cookie:
            from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415

            async with db_session() as s:
                await session_lifecycle.touch(s, session_cookie)
                await s.commit()
        # Return the resolved authz projection. The dep is consumed for its
        # side-effects (contextvars, role/SSO checks); callers that capture the
        # return value get the real `AuthOrg`, never a half-populated view.
        return auth_org

    return _dep


async def public_route(request: Request) -> None:
    """Compat re-export. The canonical definition lives in
    `core.auth.context.public_route` so non-domain modules can import it
    without layering cycles."""
    from app.core.auth import public_route as _core_public_route  # noqa: PLC0415

    await _core_public_route()


def current_actor() -> Actor:
    """Helper for handlers that need to write an audit entry. Reads the
    contextvars `require(...)` set. Raises if called before `require`."""
    user_id = user_id_var.get()
    if user_id is None:
        raise RuntimeError("current_actor() called without an authenticated session")
    return Actor.user(user_id=user_id)
