"""Session-only FastAPI dependency for `RouteSecurity.USER_SCOPED` routes.

This module owns `require_session` — the dependency that resolves a session
cookie to `user_id_var` with no org/role check. It lives in `core/identity`
because its only real dependencies are `identity_repo` (session lookup),
`core/auth` contextvars, and `core/database` — all at or below identity.
The former placement in `core/sessions` created an identity↔sessions cycle.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie

from app.core.audit_log import ActorKind
from app.core.auth import AuthFailure, actor_id_var, actor_kind_var, user_id_var
from app.core.database import session as db_session
from app.core.identity import repository as identity_repo


async def require_session(
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> None:
    """Dependency for `RouteSecurity.USER_SCOPED` routes. Requires a valid
    session cookie; sets `user_id_var`, `actor_kind_var`, `actor_id_var`.
    Does **not** require `X-Yaaos-Org-Slug` or perform any membership / role check —
    the route operates on the user, not on an org.

    Raises `AuthFailure("unauthenticated")` (→ 401, clearing cookies) when no
    session is present, the session is expired, or the token is unknown. The
    middleware has already set `route_security_resolved = "user_scoped"` based
    on path classification.
    """
    if not yaaos_session:
        raise AuthFailure("unauthenticated")
    token_hash = identity_repo.hash_token(yaaos_session)
    async with db_session() as s:
        row = await identity_repo.get_session_by_hash(s, token_hash)
    if row is None or row.user_id is None:
        raise AuthFailure("unauthenticated")
    from datetime import UTC, datetime  # noqa: PLC0415

    if row.expires_at < datetime.now(UTC):
        raise AuthFailure("unauthenticated")
    user_id_var.set(row.user_id)
    actor_kind_var.set(ActorKind.USER)
    actor_id_var.set(row.user_id)
