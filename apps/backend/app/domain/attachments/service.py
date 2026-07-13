"""Service surface for `domain/attachments`.

Owns the `ticket_attachments` table. Attachments are user-supplied ticket
inputs — text documents attached before or during a run. Frontmatter is
parsed deterministically at attach time; parse failure or absence degrades
the attachment to context-only (metadata columns remain NULL).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.domain.attachments.contracts import parse_frontmatter
from app.domain.attachments.models import TicketAttachmentRow
from app.domain.attachments.types import Attachment, AttachmentMeta
from app.domain.tickets import TicketNotFoundError as TicketsNotFoundError
from app.domain.tickets import get as _get_ticket

# 2 MiB in bytes — symmetric with the agent-side artifact size cap.
_MAX_BODY_BYTES = 2 * 1024 * 1024


class TicketNotFoundError(LookupError):
    """Raised by `add_attachment` when the ticket row is absent or org-mismatched."""


class AttachmentTooLargeError(ValueError):
    """Raised by `add_attachment` when `body` exceeds the 2 MiB cap."""


class InvalidAttachmentFilenameError(ValueError):
    """Raised by `add_attachment` when `filename` is not a single safe path segment."""


class AttachmentNotFoundError(LookupError):
    """Raised by `get_attachment` when the row is absent or org-mismatched.

    Absent and cross-org indistinguishable — same pattern as `artifacts.get`.
    """


def _validate_filename(filename: str) -> None:
    """Require `filename` to be a single, safe path segment.

    The run engine later joins the stored filename as `.yaaos-inputs/<filename>`
    into a `WriteFilesEntry` path for the agent workspace. A traversal segment
    (e.g. `../.git/hooks/pre-commit`) normalizes to a path that stays inside
    the workspace root — so the agent-side join accepts it — and lands the body
    outside the inputs directory. Validation lives here, at the storage
    boundary, so both HTTP and MCP ingress are covered by one check.
    """
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or filename == "."
        or len(PurePosixPath(filename).parts) != 1
    ):
        raise InvalidAttachmentFilenameError(filename)


# ---------------------------------------------------------------------------
# Audit payload
# ---------------------------------------------------------------------------


class _AttachmentAddedPayload(BaseModel):
    attachment_id: UUID
    filename: str
    produced_by_skill: str | None
    artifact_type: str | None


async def _audit_for_attachment(
    attachment_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession,
) -> None:
    await audit("attachment", attachment_id, kind, payload, actor, org_id=org_id, session=session)


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def add_attachment(
    ticket_id: UUID,
    *,
    org_id: UUID,
    filename: str,
    body: str,
    note: str | None = None,
    actor: Actor,
    session: AsyncSession,
) -> Attachment:
    """Store a user-supplied ticket input document.

    Parses frontmatter deterministically; parse failure / absence sets all
    metadata columns to NULL (context-only attachment). Writes an
    `attachment.added` audit row and queues an `attachment_added` SSE event
    for after-commit publication.

    Raises `TicketNotFoundError` when `ticket_id` does not exist in `org_id`.
    Raises `AttachmentTooLargeError` when `body` exceeds 2 MiB.
    Raises `InvalidAttachmentFilenameError` when `filename` is not a single
    safe path segment.
    """
    # Filename safety — enforced at the storage boundary so both HTTP and MCP
    # ingress are covered.
    _validate_filename(filename)

    # Size cap — enforced before any DB write.
    if len(body.encode("utf-8")) > _MAX_BODY_BYTES:
        raise AttachmentTooLargeError("body exceeds 2 MiB limit")

    # Ticket existence check via the tickets module's public API.
    # `tickets.get` opens its own session (shape b); the FK constraint on the
    # write below is the ultimate atomicity guard.
    try:
        await _get_ticket(ticket_id, org_id=org_id)
    except TicketsNotFoundError:
        raise TicketNotFoundError(str(ticket_id))

    # Deterministic frontmatter parse — never raises; None = no/invalid frontmatter.
    fm = parse_frontmatter(body)

    # `attached_by` records the user who performed the attach. For user actors
    # (the only kind the HTTP endpoint accepts) `user_id` is always set.
    attached_by: UUID = actor.user_id if actor.user_id is not None else UUID(int=0)

    row = TicketAttachmentRow(
        org_id=org_id,
        ticket_id=ticket_id,
        filename=filename,
        body=body,
        produced_by_skill=fm.skill if fm is not None else None,
        skill_version=fm.skill_version if fm is not None else None,
        artifact_type=fm.artifact_type if fm is not None else None,
        produced_at=fm.produced_at if fm is not None else None,
        repo_commit=fm.repo_commit if fm is not None else None,
        produced_from=fm.produced_from if fm is not None else None,
        note=note,
        attached_by=attached_by,
    )
    session.add(row)
    await session.flush()

    await _audit_for_attachment(
        row.id,
        "attachment.added",
        _AttachmentAddedPayload(
            attachment_id=row.id,
            filename=filename,
            produced_by_skill=row.produced_by_skill,
            artifact_type=row.artifact_type,
        ),
        actor=actor,
        org_id=org_id,
        session=session,
    )

    publish_general_after_commit(
        session,
        org_id=org_id,
        kind=GeneralEventKind.ATTACHMENT_ADDED,
        payload={"ticket_id": str(ticket_id), "attachment_id": str(row.id)},
    )

    return row.to_attachment()


async def list_attachments(
    ticket_id: UUID,
    *,
    org_id: UUID,
    session: AsyncSession,
) -> list[AttachmentMeta]:
    """Return attachment metadata for a ticket, newest first (no bodies).

    Name is deliberately `list_attachments`, not `list_for_ticket`, because
    `domain/artifacts` already exports a `list_for_ticket` and future callers
    may import both modules without aliasing.
    """
    rows = (
        (
            await session.execute(
                select(TicketAttachmentRow)
                .where(
                    TicketAttachmentRow.ticket_id == ticket_id,
                    TicketAttachmentRow.org_id == org_id,
                )
                .order_by(TicketAttachmentRow.attached_at.desc(), TicketAttachmentRow.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [r.to_meta() for r in rows]


async def latest_matching(
    ticket_id: UUID,
    *,
    skill_name: str,
    attachment_ids: Sequence[UUID],
    session: AsyncSession,
) -> Attachment | None:
    """Return the newest attachment in `attachment_ids` whose
    `produced_by_skill` equals `skill_name`, or None when none match.

    Used by the run engine's adoption fork to find a candidate attachment for
    an upcoming skill stage. `attachment_ids` is the run's kickoff snapshot —
    already scoped to the right ticket and run.
    """
    if not attachment_ids:
        return None
    row = (
        await session.execute(
            select(TicketAttachmentRow)
            .where(
                TicketAttachmentRow.ticket_id == ticket_id,
                TicketAttachmentRow.id.in_(attachment_ids),
                TicketAttachmentRow.produced_by_skill == skill_name,
            )
            .order_by(TicketAttachmentRow.attached_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.to_attachment() if row is not None else None


async def get_attachment(
    attachment_id: UUID,
    *,
    org_id: UUID,
    session: AsyncSession,
) -> Attachment:
    """Return the attachment with body, scoped to `org_id`.

    Raises `AttachmentNotFoundError` when absent OR in a different org —
    cross-org existence is not leaked (same pattern as `artifacts.get`).
    """
    row = (
        await session.execute(
            select(TicketAttachmentRow).where(
                TicketAttachmentRow.id == attachment_id,
                TicketAttachmentRow.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AttachmentNotFoundError(str(attachment_id))
    return row.to_attachment()
