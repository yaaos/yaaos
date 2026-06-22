"""HTTP wiring for `core/notifications`.

| Method | Path                                | Auth                    |
|--------|-------------------------------------|-------------------------|
| GET    | `/api/notifications`                | session-only (per-user) |
| POST   | `/api/notifications/{id}/read`      | session-only            |
| POST   | `/api/notifications/mark-read`      | session-only            |
| GET    | `/api/notifications/popover`        | session-only            |

These endpoints are user-scoped (cross-org): the session cookie identifies
the recipient; org filters are optional query params, not header context.
The whole prefix lives on the public allowlist in `core/auth/types.py` so
the auth middleware doesn't demand `X-Yaaos-Org-Slug`; we manually resolve the
session cookie inside each handler (same pattern as `/api/orgs/mine`).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.auth import public_route
from app.core.database import session as db_session
from app.core.identity import find_session_by_hash, hash_token
from app.core.notifications import service as notif_service
from app.core.webserver import RouteSpec, register_routes

router = APIRouter(dependencies=[Depends(public_route)])


class _MarkReadFilter(BaseModel):
    read_state: str | None = None
    org_id: UUID | None = None
    types: list[str] | None = None


async def _resolve_user(yaaos_session: str | None) -> UUID | None:
    if not yaaos_session:
        return None
    token_hash = hash_token(yaaos_session)
    async with db_session() as s:
        sess = await find_session_by_hash(s, token_hash)
    if sess is None or sess.user_id is None:
        return None
    return sess.user_id


def _unauth() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "unauthenticated"})


@router.get("")
async def list_(
    yaaos_session: Annotated[str | None, Cookie()] = None,
    read_state: str = Query(default="all"),
    org_id: UUID | None = Query(default=None),
    types: list[str] | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> JSONResponse:
    user_id = await _resolve_user(yaaos_session)
    if user_id is None:
        return _unauth()
    rs = read_state if read_state in ("all", "unread", "read") else "all"
    async with db_session() as s:
        rows = await notif_service.list_for_user(
            s,
            user_id=user_id,
            read_state=rs,  # type: ignore[arg-type]
            org_id=org_id,
            types=types,
            limit=limit,
        )
    return JSONResponse(content=[r.model_dump(mode="json") for r in rows])


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: UUID,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> JSONResponse:
    user_id = await _resolve_user(yaaos_session)
    if user_id is None:
        return _unauth()
    async with db_session() as s:
        row = await notif_service.mark_read(s, user_id=user_id, notification_id=notification_id)
        if row is None:
            return JSONResponse(status_code=404, content={"error": "notification not found"})
        await s.commit()
    return JSONResponse(content=row.model_dump(mode="json"))


@router.post("/mark-read")
async def mark_all_read(
    body: _MarkReadFilter,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> JSONResponse:
    user_id = await _resolve_user(yaaos_session)
    if user_id is None:
        return _unauth()
    async with db_session() as s:
        marked = await notif_service.mark_all_read(s, user_id=user_id, org_id=body.org_id, types=body.types)
        await s.commit()
    return JSONResponse(content={"marked": marked})


@router.get("/popover")
async def popover(
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> JSONResponse:
    user_id = await _resolve_user(yaaos_session)
    if user_id is None:
        return _unauth()
    async with db_session() as s:
        rows, unread = await notif_service.popover_for_user(s, user_id=user_id, limit=10)
    return JSONResponse(
        content={
            "items": [r.model_dump(mode="json") for r in rows],
            "unread_count": unread,
        }
    )


register_routes(
    RouteSpec(
        module_name="notifications",
        router=router,
        url_prefix="/api/notifications",
    )
)
