"""HTTP routes for `/api/user/*` — `RouteSecurity.USER_SCOPED`.

| Method | Path                                    | Action              |
|--------|-----------------------------------------|---------------------|
| GET    | `/api/user/emails`                      | `USER_UPDATE_SELF`  |
| POST   | `/api/user/emails`                      | `USER_UPDATE_SELF`  |
| DELETE | `/api/user/emails/{email_id}`           | `USER_UPDATE_SELF`  |
| GET    | `/api/user/me`                          | `USER_UPDATE_SELF` — user profile (display_name, github_username, emails, per-org handles) |
| PATCH  | `/api/user/me`                          | `USER_UPDATE_SELF` — update display_name; clear github_username |

Session is enforced by `_require_user()`. No `X-Org-Slug` header is
required — these endpoints operate on the user, not on a single org.

`users.github_username` is written automatically by the "Sign in with
GitHub" login flow. Re-binding to a different GitHub account is "sign in
with GitHub again" — no dedicated endpoint.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import MUTATE_LIMIT, limiter, user_id_var
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("identity.user.web")

router = APIRouter()


class _AddEmailRequest(BaseModel):
    email: str


class EmailView(BaseModel):
    id: UUID
    email: str
    is_primary: bool
    verified: bool


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _require_user():
    """Session-only auth: resolves the cookie → `user_id_var`. No org context.
    Lazy import because `core/sessions` depends on `core/identity`."""
    from app.core.sessions import require_session  # noqa: PLC0415

    return require_session


@router.get("/emails", dependencies=[Depends(_require_user())])
async def list_emails() -> list[EmailView]:
    from app.core.identity import repository as identity_repo  # noqa: PLC0415

    user_id = user_id_var.get()
    if user_id is None:
        raise _err(401, "unauthenticated")
    async with db_session() as s:
        rows = await identity_repo.list_emails_for_user(s, user_id)
    return [
        EmailView(id=r.id, email=r.email, is_primary=r.is_primary, verified=r.verified_at is not None)
        for r in rows
    ]


@router.post("/emails", dependencies=[Depends(_require_user())])
@limiter.limit(MUTATE_LIMIT)
async def add_email(
    request: Request,
    body: _AddEmailRequest,
    yaaos_csrf: Annotated[str | None, Cookie()] = None,
) -> EmailView:
    from app.core.identity import repository as identity_repo  # noqa: PLC0415

    user_id = user_id_var.get()
    if user_id is None:
        raise _err(401, "unauthenticated")
    async with db_session() as s:
        row = await identity_repo.add_email(
            s, user_id=user_id, email=body.email.lower(), is_primary=False, verified=False
        )
        await s.commit()
    return EmailView(id=row.id, email=row.email, is_primary=row.is_primary, verified=False)


@router.delete("/emails/{email_id}", dependencies=[Depends(_require_user())])
@limiter.limit(MUTATE_LIMIT)
async def remove_email(request: Request, email_id: UUID) -> dict[str, str]:
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.identity import repository as identity_repo  # noqa: PLC0415
    from app.core.identity.models import UserEmailRow  # noqa: PLC0415

    user_id = user_id_var.get()
    if user_id is None:
        raise _err(401, "unauthenticated")
    async with db_session() as s:
        row = (
            await s.execute(
                select(UserEmailRow).where(UserEmailRow.id == email_id, UserEmailRow.user_id == user_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise _err(404, "email_not_found")
        # Last-verified-email invariant.
        if row.verified_at is not None:
            verified_count = await identity_repo.count_verified_emails(s, user_id)
            if verified_count <= 1:
                raise _err(409, "last_verified_email")
        deleted = await identity_repo.delete_email(s, user_id=user_id, email_id=email_id)
        if not deleted:
            raise _err(404, "email_not_found")
        await s.commit()
    return {"ok": "deleted"}


class _UserMeResponse(BaseModel):
    user_id: UUID
    display_name: str
    github_username: str | None
    emails: list[EmailView]
    memberships: list[dict]  # [{org_id, slug, display_name, role, handle}]


class _PatchUserRequest(BaseModel):
    display_name: str | None = None
    # If True, clears users.github_username. Setting it is done by signing
    # in with GitHub — never directly.
    clear_github_username: bool = False


@router.get("/me", dependencies=[Depends(_require_user())])
async def user_me() -> _UserMeResponse:
    """Profile payload for the User > Details page."""
    from app.core.identity import repository as identity_repo  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    user_id = user_id_var.get()
    if user_id is None:
        raise _err(401, "unauthenticated")
    async with db_session() as s:
        user = await identity_repo.get_user(s, user_id)
        if user is None:
            raise _err(404, "user_not_found")
        emails = await identity_repo.list_emails_for_user(s, user_id)
        rows = await orgs_repo.list_memberships_for_user(s, user_id)
        memberships_view = []
        for m in rows:
            org = await orgs_repo.get_org(s, m.org_id)
            if org is None:
                continue
            memberships_view.append(
                {
                    "org_id": str(org.id),
                    "slug": org.slug,
                    "display_name": org.display_name,
                    "role": m.role,
                    "handle": m.handle,
                }
            )
    return _UserMeResponse(
        user_id=user.id,
        display_name=user.display_name,
        github_username=user.github_username,
        emails=[
            EmailView(id=r.id, email=r.email, is_primary=r.is_primary, verified=r.verified_at is not None)
            for r in emails
        ],
        memberships=memberships_view,
    )


@router.patch("/me", dependencies=[Depends(_require_user())])
@limiter.limit(MUTATE_LIMIT)
async def patch_user_me(
    request: Request,
    body: _PatchUserRequest,
    yaaos_csrf: Annotated[str | None, Cookie()] = None,
) -> _UserMeResponse:
    """Update profile fields. `display_name` accepted as plain text; the
    `github_username` denorm is owned by the login flow and is never written
    here. `clear_github_username=true` removes the verified value."""
    from app.core.identity import repository as identity_repo  # noqa: PLC0415

    user_id = user_id_var.get()
    if user_id is None:
        raise _err(401, "unauthenticated")
    async with db_session() as s:
        if body.display_name is not None:
            await identity_repo.set_user_display_name(s, user_id=user_id, display_name=body.display_name)
        if body.clear_github_username:
            await identity_repo.set_user_github_username(s, user_id=user_id, github_username=None)
        await s.commit()
    return await user_me()


register_routes(RouteSpec(module_name="user", router=router, url_prefix="/api/user"))


__all__ = ["router"]
