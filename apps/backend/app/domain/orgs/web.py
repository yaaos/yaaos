"""HTTP wiring for `domain/orgs` — `/api/memberships/*` endpoints.

| Method | Path | Action |
|---|---|---|
| GET    | `/api/memberships`                  | `MEMBERS_READ` — list members of the current org. |
| POST   | `/api/memberships/invite`           | `MEMBERS_INVITE` — invite by email. |
| POST   | `/api/memberships/accept`           | public (authenticated by session cookie only) — accept a signed invitation token. |
| PATCH  | `/api/memberships/{user_id}`        | `MEMBERS_CHANGE_ROLE` — change role; rotates target sessions. |
| DELETE | `/api/memberships/{user_id}`        | `MEMBERS_REMOVE` — revoke membership + every session. |

Accept is intentionally minimal-auth: it must work for users who don't yet
have a membership in the org. Session cookie identifies who is accepting;
the signed token authorizes the action.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import AUTH_LIMIT, MUTATE_LIMIT, Action, limiter, org_id_var, user_id_var
from app.core.database import session as db_session
from app.core.identity import repository as identity_repo
from app.core.sessions import current_actor, public_route, require
from app.core.webserver import RouteSpec, register_routes
from app.domain.orgs import invitations as inv
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.service import Membership
from app.domain.orgs.types import InvitationError, Role

log = structlog.get_logger("orgs.web")

router = APIRouter()


class InviteRequest(BaseModel):
    email: str
    role: Role


class InviteResponse(BaseModel):
    invitation_id: UUID
    email: str
    role: Role
    expires_at: str


class AcceptRequest(BaseModel):
    token: str


class ChangeRoleRequest(BaseModel):
    role: Role


class MemberView(BaseModel):
    user_id: UUID
    handle: str
    role: Role
    display_name: str
    primary_email: str | None


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("", dependencies=[Depends(require(Action.MEMBERS_READ))])
async def list_members() -> list[MemberView]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        rows = await orgs_repo.list_memberships_for_org(s, org_id)
        out: list[MemberView] = []
        for row in rows:
            user = await identity_repo.get_user(s, row.user_id)
            emails = await identity_repo.list_emails_for_user(s, row.user_id)
            primary = next((e for e in emails if e.is_primary), emails[0] if emails else None)
            out.append(
                MemberView(
                    user_id=row.user_id,
                    handle=row.handle,
                    role=Role(row.role),
                    display_name=user.display_name if user else "",
                    primary_email=primary.email if primary else None,
                )
            )
    return out


@router.post("/invite", dependencies=[Depends(require(Action.MEMBERS_INVITE))])
@limiter.limit(MUTATE_LIMIT)
async def invite_member(request: Request, body: InviteRequest) -> InviteResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        invitation, _raw = await inv.invite(
            s,
            org_id=org_id,
            email=str(body.email),
            role=body.role,
            invited_by_user_id=actor.user_id,
            actor=actor,
        )
        await s.commit()
    return InviteResponse(
        invitation_id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at.isoformat(),
    )


@router.post("/accept", dependencies=[Depends(public_route)])
@limiter.limit(AUTH_LIMIT)
async def accept_invitation(
    request: Request,
    body: AcceptRequest,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Membership:
    """Public-allowlist endpoint — session cookie identifies the acceptor."""
    if not yaaos_session:
        raise _err(401, "unauthenticated")
    async with db_session() as s:
        session = await _resolve_session_user(s, yaaos_session)
        if session is None:
            raise _err(401, "unauthenticated")
        user_id = session
        from app.core.audit_log import Actor  # noqa: PLC0415

        actor = Actor.user(user_id=user_id)
        try:
            membership = await inv.accept_invitation(s, raw_token=body.token, user_id=user_id, actor=actor)
        except inv.InvitationExpiredError:
            raise _err(410, "invitation_expired")
        except inv.InvitationUsedError:
            raise _err(410, "invitation_used")
        except InvitationError:
            raise _err(400, "invitation_invalid")
        await s.commit()
    return membership


@router.delete("/{target_user_id}", dependencies=[Depends(require(Action.MEMBERS_REMOVE))])
@limiter.limit(MUTATE_LIMIT)
async def remove_member(request: Request, target_user_id: UUID) -> dict[str, str]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        await inv.remove_member(s, org_id=org_id, user_id=target_user_id, actor=actor)
        await s.commit()
    return {"ok": "removed"}


class _PatchOwnHandleRequest(BaseModel):
    handle: str


@router.patch("/me/{org_id}", dependencies=[Depends(require(Action.USER_UPDATE_SELF))])
@limiter.limit(MUTATE_LIMIT)
async def patch_own_membership_handle(
    request: Request,
    org_id: UUID,
    body: _PatchOwnHandleRequest,
) -> Membership:
    """Self-update one's `@handle` in the org named by the path param.
    Enforces `UNIQUE(org_id, handle)` via the existing partial index —
    duplicate handles surface as 409. The path `org_id` may differ from
    `X-Org-Slug` (which the middleware still requires for the prefix gate)."""
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    self_user_id = user_id_var.get()
    if self_user_id is None:
        raise _err(401, "unauthenticated")
    handle = body.handle.strip()
    if not handle or len(handle) > 64:
        raise _err(422, "invalid_handle")
    async with db_session() as s:
        membership_row = await orgs_repo.get_membership(s, user_id=self_user_id, org_id=org_id)
        if membership_row is None:
            raise _err(404, "membership_not_found")
        membership_row.handle = handle
        try:
            await s.flush()
        except IntegrityError as exc:
            raise _err(409, "handle_taken") from exc
        await s.commit()
        await s.refresh(membership_row)
    return Membership.from_row(membership_row)


@router.patch("/{target_user_id}", dependencies=[Depends(require(Action.MEMBERS_CHANGE_ROLE))])
@limiter.limit(MUTATE_LIMIT)
async def change_role(request: Request, target_user_id: UUID, body: ChangeRoleRequest) -> Membership:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        try:
            membership = await inv.change_role(
                s,
                org_id=org_id,
                user_id=target_user_id,
                new_role=body.role,
                actor=actor,
            )
        except LookupError:
            raise _err(404, "membership_not_found")
        await s.commit()
    return membership


async def _resolve_session_user(s, raw_token: str) -> UUID | None:
    """Look up the user_id behind a session cookie. Inlined here because
    `accept_invitation` runs without the `require()` dep that normally
    populates `user_id_var`."""
    token_hash = identity_repo.hash_token(raw_token)
    row = await identity_repo.get_session_by_hash(s, token_hash)
    if row is None or row.user_id is None:
        return None
    from datetime import UTC, datetime  # noqa: PLC0415

    if row.expires_at < datetime.now(UTC):
        return None
    user_id_var.set(row.user_id)
    return row.user_id


register_routes(RouteSpec(module_name="memberships", router=router))


__all__ = ["router"]
