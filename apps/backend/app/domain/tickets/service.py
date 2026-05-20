"""Ticket aggregate — yaaos's unit of work."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select, update

from app.core.audit_log import Actor, audit_for_ticket
from app.core.database import session as db_session
from app.core.events import Event, publish
from app.domain.tickets.models import TicketRow

TicketStatus = Literal["open", "in_review", "complete", "abandoned"]


class Ticket(BaseModel):
    id: UUID
    org_id: UUID
    source: str
    source_external_id: str
    title: str
    description: str | None
    status: TicketStatus
    plugin_id: str
    repo_external_id: str
    pr_id: UUID | None
    # Enriched fields (denormalized at read-time from the linked PR; nullable
    # because a ticket can briefly exist without its PR row in the create flow).
    pr_number: int | None = None
    pr_html_url: str | None = None
    author_login: str | None = None
    is_draft: bool | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: TicketRow) -> Ticket:
        return cls(
            id=row.id,
            org_id=row.org_id,
            source=row.source,
            source_external_id=row.source_external_id,
            title=row.title,
            description=row.description,
            status=row.status,  # type: ignore[arg-type]
            plugin_id=row.plugin_id,
            repo_external_id=row.repo_external_id,
            pr_id=row.pr_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class TicketFilter(BaseModel):
    repo_external_ids: list[str] | None = None
    author_logins: list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    statuses: list[TicketStatus] | None = None


class TicketStatusChanged(Event):
    kind: Literal["ticket_status_changed"] = "ticket_status_changed"
    source_module: Literal["tickets"] = "tickets"
    repo_external_id: str
    pr_id: UUID | None
    previous_status: str | None
    new_status: str
    reason: str | None = None


class TicketNotFoundError(LookupError):
    pass


class InvalidTicketTransition(ValueError):
    pass


class _TicketCreatedPayload(BaseModel):
    pr_id: UUID
    repo_external_id: str


class _TicketStatusChangedPayload(BaseModel):
    from_status: str
    to_status: str
    reason: str | None = None


async def create_for_pr(
    repo_external_id: str,
    source_external_id: str,
    title: str,
    description: str | None,
    pr_id: UUID,
    *,
    org_id: UUID,
    plugin_id: str = "github",
) -> Ticket:
    """Idempotent: if a ticket exists for pr_id, return it."""
    async with db_session() as s:
        existing = (
            await s.execute(select(TicketRow).where(TicketRow.pr_id == pr_id, TicketRow.org_id == org_id))
        ).scalar_one_or_none()
        if existing is not None:
            existing.title = title
            existing.description = description
            await s.commit()
            await s.refresh(existing)
            return Ticket.from_row(existing)
        row = TicketRow(
            id=uuid4(),
            org_id=org_id,
            source="github_pr",
            source_external_id=source_external_id,
            title=title,
            description=description,
            status="in_review",
            plugin_id=plugin_id,
            repo_external_id=repo_external_id,
            pr_id=pr_id,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        row_id = row.id

    await audit_for_ticket(
        row_id,
        "ticket.created",
        _TicketCreatedPayload(pr_id=pr_id, repo_external_id=repo_external_id),
        actor=Actor.system(),
        org_id=org_id,
    )
    await publish(
        TicketStatusChanged(
            ticket_id=row_id,
            repo_external_id=repo_external_id,
            pr_id=pr_id,
            previous_status=None,
            new_status="in_review",
        )
    )
    return await get(row_id, org_id=org_id)


async def get(ticket_id: UUID, *, org_id: UUID) -> Ticket:
    from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        t = Ticket.from_row(row)
        if row.pr_id is not None:
            pr = (
                await s.execute(select(PullRequestRow).where(PullRequestRow.id == row.pr_id))
            ).scalar_one_or_none()
            if pr is not None:
                t.pr_number = pr.number
                t.pr_html_url = pr.html_url
                t.author_login = pr.author_login
                t.is_draft = pr.is_draft
    return t


async def get_by_pr(pr_id: UUID, *, org_id: UUID) -> Ticket | None:
    async with db_session() as s:
        row = (
            await s.execute(select(TicketRow).where(TicketRow.pr_id == pr_id, TicketRow.org_id == org_id))
        ).scalar_one_or_none()
    return Ticket.from_row(row) if row is not None else None


async def list_tickets(
    filter: TicketFilter,
    *,
    org_id: UUID,
    limit: int = 50,
) -> list[Ticket]:
    from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415

    async with db_session() as s:
        stmt = (
            select(TicketRow)
            .where(TicketRow.org_id == org_id)
            .order_by(TicketRow.updated_at.desc())
            .limit(limit)
        )
        if filter.repo_external_ids:
            stmt = stmt.where(TicketRow.repo_external_id.in_(filter.repo_external_ids))
        if filter.statuses:
            stmt = stmt.where(TicketRow.status.in_(filter.statuses))
        if filter.created_after is not None:
            stmt = stmt.where(TicketRow.created_at >= filter.created_after)
        if filter.created_before is not None:
            stmt = stmt.where(TicketRow.created_at < filter.created_before)
        rows = (await s.execute(stmt)).scalars().all()
        # Batch-enrich with PR data (one query, not N+1).
        pr_ids = [r.pr_id for r in rows if r.pr_id is not None]
        prs_by_id: dict[UUID, PullRequestRow] = {}
        if pr_ids:
            pr_rows = (
                (await s.execute(select(PullRequestRow).where(PullRequestRow.id.in_(pr_ids)))).scalars().all()
            )
            prs_by_id = {p.id: p for p in pr_rows}
        out: list[Ticket] = []
        for r in rows:
            t = Ticket.from_row(r)
            if r.pr_id and r.pr_id in prs_by_id:
                pr = prs_by_id[r.pr_id]
                t.pr_number = pr.number
                t.pr_html_url = pr.html_url
                t.author_login = pr.author_login
                t.is_draft = pr.is_draft
            out.append(t)
        return out


async def complete(ticket_id: UUID, *, org_id: UUID) -> None:
    await _transition(ticket_id, new_status="complete", org_id=org_id)


async def abandon(ticket_id: UUID, *, reason: str, org_id: UUID) -> None:
    await _transition(ticket_id, new_status="abandoned", org_id=org_id, reason=reason)


async def _transition(
    ticket_id: UUID,
    *,
    new_status: TicketStatus,
    org_id: UUID,
    reason: str | None = None,
) -> None:
    async with db_session() as s:
        row = (
            await s.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        if row.status in ("complete", "abandoned"):
            raise InvalidTicketTransition(f"ticket {ticket_id} is terminal ({row.status}); cannot transition")
        prev = row.status
        await s.execute(update(TicketRow).where(TicketRow.id == ticket_id).values(status=new_status))
        await s.commit()
        repo_external_id = row.repo_external_id
        pr_id = row.pr_id

    await audit_for_ticket(
        ticket_id,
        "ticket.status_changed",
        _TicketStatusChangedPayload(from_status=prev, to_status=new_status, reason=reason),
        actor=Actor.system(),
        org_id=org_id,
    )
    await publish(
        TicketStatusChanged(
            ticket_id=ticket_id,
            repo_external_id=repo_external_id,
            pr_id=pr_id,
            previous_status=prev,
            new_status=new_status,
            reason=reason,
        )
    )
