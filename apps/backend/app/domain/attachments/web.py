"""HTTP wiring for `domain/attachments`.

| Method | Path                    | Action           |
|--------|-------------------------|------------------|
| POST   | `/api/attachments`      | `REVIEWER_WRITE` — store an attachment against a ticket |
| GET    | `/api/attachments`      | `TICKETS_READ`   — list attachment metadata for a ticket |
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.auth import Action, org_id_var
from app.core.database import session as db_session
from app.core.sessions import current_actor, require
from app.core.webserver import RouteSpec, register_routes
from app.domain.attachments.service import (
    AttachmentTooLargeError,
    TicketNotFoundError,
    add_attachment,
    list_attachments,
)
from app.domain.attachments.types import AttachmentMeta

router = APIRouter()


def _err(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": code})


def _org() -> UUID:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    return org_id


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class PostAttachmentRequest(BaseModel):
    ticket_id: UUID
    filename: str
    body: str
    note: str | None = None


class PostAttachmentResponse(BaseModel):
    id: UUID
    filename: str
    produced_by_skill: str | None
    skill_version: str | None
    artifact_type: str | None
    repo_commit: str | None
    attached_at: datetime


class ListAttachmentsResponse(BaseModel):
    attachments: list[AttachmentMeta]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", dependencies=[Depends(require(Action.REVIEWER_WRITE))], status_code=status.HTTP_201_CREATED)
async def post_attachment(body: PostAttachmentRequest) -> PostAttachmentResponse:
    org_id = _org()
    actor = current_actor()
    async with db_session() as s:
        try:
            attachment = await add_attachment(
                body.ticket_id,
                org_id=org_id,
                filename=body.filename,
                body=body.body,
                note=body.note,
                actor=actor,
                session=s,
            )
            await s.commit()
        except TicketNotFoundError as exc:
            raise _err(404, "ticket_not_found") from exc
        except AttachmentTooLargeError as exc:
            raise _err(413, "too_large") from exc
    return PostAttachmentResponse(
        id=attachment.id,
        filename=attachment.filename,
        produced_by_skill=attachment.produced_by_skill,
        skill_version=attachment.skill_version,
        artifact_type=attachment.artifact_type,
        repo_commit=attachment.repo_commit,
        attached_at=attachment.attached_at,
    )


@router.get("", dependencies=[Depends(require(Action.TICKETS_READ))])
async def list_attachments_endpoint(ticket_id: UUID = Query(...)) -> ListAttachmentsResponse:
    org_id = _org()
    async with db_session() as s:
        metas = await list_attachments(ticket_id, org_id=org_id, session=s)
    return ListAttachmentsResponse(attachments=metas)


register_routes(
    RouteSpec(
        module_name="attachments",
        router=router,
        url_prefix="/api/attachments",
    )
)
