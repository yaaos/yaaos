"""Ticket aggregate — yaaos's unit of work."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_ticket
from app.core.database import session as db_session
from app.core.events import Event, publish
from app.domain.tickets.models import TicketRow

TicketStatus = Literal["open", "in_review", "complete", "abandoned"]


M06Status = Literal["running", "hitl", "done", "failed", "cancelled"]


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
    # M06 fields (nullable until the projections that populate them ship).
    m06_status: M06Status | None = None
    current_stage: str | None = None
    findings_count: int = 0
    max_severity: Literal["low", "medium", "high"] | None = None
    builder_kind: Literal["user", "system"] = "user"
    builder_display_name: str | None = None

    @classmethod
    def from_row(cls, row: TicketRow) -> Ticket:
        from app.domain.tickets.m06_status import project_status  # noqa: PLC0415

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
            m06_status=project_status(row.status),
        )


TicketSort = Literal["updated_desc", "updated_asc", "created_desc", "status", "findings_count"]


class TicketFilter(BaseModel):
    repo_external_ids: list[str] | None = None
    author_logins: list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    statuses: list[TicketStatus] | None = None
    # M06 additions — see `plan/milestones/M06-design-refresh/api-changes.md`.
    q: str | None = None
    sort: TicketSort = "updated_desc"
    cursor: str | None = None


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


async def create(
    *,
    type: str,
    payload: dict,
    idempotency_key: str,
    org_id: UUID,
    title: str | None = None,
    description: str | None = None,
    source: str = "intake",
    source_external_id: str | None = None,
    plugin_id: str = "github",
    repo_external_id: str = "",
    session: AsyncSession,
) -> tuple[UUID, bool]:
    """Create a ticket from an intake event, or return the existing one if
    `idempotency_key` already produced a ticket. Required `session`; the
    caller commits. Returns `(ticket_id, created)` — `created=False` on
    idempotent return.

    The ticket starts in status `pending`; the workflow engine moves it to
    `running` when the first step dispatches and to `done|failed|cancelled`
    on terminal outcome.
    """
    if not idempotency_key:
        raise ValueError("idempotency_key required")
    if not type:
        raise ValueError("type required")

    existing = (
        await session.execute(
            select(TicketRow).where(
                TicketRow.org_id == org_id,
                TicketRow.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id, False

    row = TicketRow(
        id=uuid4(),
        org_id=org_id,
        source=source,
        source_external_id=source_external_id or idempotency_key,
        title=title or "",
        description=description,
        status="pending",
        plugin_id=plugin_id,
        repo_external_id=repo_external_id,
        pr_id=None,
        type=type,
        idempotency_key=idempotency_key,
        payload=payload,
        current_workflow_execution_id=None,
    )
    session.add(row)
    await session.flush()
    await audit_for_ticket(
        row.id,
        "ticket.created",
        _TicketCreatedFromIntakePayload(type=type, idempotency_key=idempotency_key),
        actor=Actor.system(),
        org_id=org_id,
        session=session,
    )
    return row.id, True


async def attach_workflow_execution(
    ticket_id: UUID,
    workflow_execution_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Stamp the canonical workflow execution onto the ticket. Caller commits."""
    await session.execute(
        update(TicketRow)
        .where(TicketRow.id == ticket_id)
        .values(current_workflow_execution_id=workflow_execution_id)
    )


class _TicketCreatedFromIntakePayload(BaseModel):
    type: str
    idempotency_key: str


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
        await s.flush()
        row_id = row.id
        await audit_for_ticket(
            row_id,
            "ticket.created",
            _TicketCreatedPayload(pr_id=pr_id, repo_external_id=repo_external_id),
            actor=Actor.system(),
            org_id=org_id,
            session=s,
        )
        await s.commit()
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


async def get_payload(ticket_id: UUID, *, session: AsyncSession) -> dict[str, Any]:
    """Return the ticket's intake payload dict. Required session — read-only,
    no commits. M05 reviewer commands read admission signals (`is_draft`,
    `is_fork`, `labels`, etc.) from here without re-fetching from GitHub."""
    row = (await session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one_or_none()
    if row is None:
        raise TicketNotFoundError(str(ticket_id))
    return dict(row.payload or {})


async def get_workspace_ticket_context(ticket_id: UUID):  # type: ignore[no-untyped-def]
    """Read the ticket fields a Workspace WorkflowCommand needs to build a
    `WorkspaceSpec` — `org_id`, `plugin_id`, `repo_external_id`, `payload`.
    Returns None when the ticket is missing.

    Owns its session (read-only, no commits). Registered with
    `core/workspace.register_workflow_context_provider` at boot so
    `ProvisionWorkspace` can read tickets without crossing the
    core → domain layer boundary.
    """
    from app.core.workspace import WorkspaceTicketContext  # noqa: PLC0415

    async with db_session() as s:
        row = (await s.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one_or_none()
    if row is None:
        return None
    return WorkspaceTicketContext(
        org_id=row.org_id,
        plugin_id=row.plugin_id or "github",
        repo_external_id=row.repo_external_id or "",
        payload=dict(row.payload or {}),
        pr_id=row.pr_id,
    )


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
    """List tickets for the org, applying the requested filters + sort.

    `findings_count` + `max_severity` are computed by a grouped query over
    `findings` rather than denormalized on the ticket row — POC simpler than
    maintaining a trigger-fed column, and the result set is small.
    """
    from sqlalchemy import case, func  # noqa: PLC0415

    from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415

    async with db_session() as s:
        stmt = select(TicketRow).where(TicketRow.org_id == org_id)
        if filter.repo_external_ids:
            stmt = stmt.where(TicketRow.repo_external_id.in_(filter.repo_external_ids))
        if filter.statuses:
            stmt = stmt.where(TicketRow.status.in_(filter.statuses))
        if filter.created_after is not None:
            stmt = stmt.where(TicketRow.created_at >= filter.created_after)
        if filter.created_before is not None:
            stmt = stmt.where(TicketRow.created_at < filter.created_before)
        if filter.q:
            stmt = stmt.where(TicketRow.title.ilike(f"%{filter.q}%"))

        sort_clause = {
            "updated_desc": TicketRow.updated_at.desc(),
            "updated_asc": TicketRow.updated_at.asc(),
            "created_desc": TicketRow.created_at.desc(),
            "status": TicketRow.status.asc(),
        }.get(filter.sort, TicketRow.updated_at.desc())
        # `findings_count` sort is handled post-fetch — keyset-paginating on a
        # computed aggregate isn't worth the SQL gymnastics in POC.
        stmt = stmt.order_by(sort_clause).limit(limit)

        rows = (await s.execute(stmt)).scalars().all()

        # Batch-enrich PR data (one query, not N+1).
        pr_ids = [r.pr_id for r in rows if r.pr_id is not None]
        prs_by_id: dict[UUID, PullRequestRow] = {}
        if pr_ids:
            pr_rows = (
                (await s.execute(select(PullRequestRow).where(PullRequestRow.id.in_(pr_ids)))).scalars().all()
            )
            prs_by_id = {p.id: p for p in pr_rows}

        # Batch-aggregate findings per pr_id. Cheap GROUP BY scoped to the
        # listed tickets' PRs; one query regardless of result-set size.
        findings_by_pr: dict[UUID, tuple[int, str | None]] = {}
        if pr_ids:
            severity_rank = case(
                (FindingRow.severity == "high", 3),
                (FindingRow.severity == "medium", 2),
                (FindingRow.severity == "low", 1),
                else_=0,
            )
            agg_stmt = (
                select(
                    FindingRow.pr_id,
                    func.count(FindingRow.id),
                    func.max(severity_rank),
                )
                .where(FindingRow.pr_id.in_(pr_ids), FindingRow.org_id == org_id)
                .group_by(FindingRow.pr_id)
            )
            for pr_id, count, max_rank in (await s.execute(agg_stmt)).all():
                severity = {3: "high", 2: "medium", 1: "low"}.get(int(max_rank or 0))
                findings_by_pr[pr_id] = (int(count), severity)

        out: list[Ticket] = []
        for r in rows:
            t = Ticket.from_row(r)
            if r.pr_id and r.pr_id in prs_by_id:
                pr = prs_by_id[r.pr_id]
                t.pr_number = pr.number
                t.pr_html_url = pr.html_url
                t.author_login = pr.author_login
                t.is_draft = pr.is_draft
                t.builder_display_name = pr.author_login
            if r.pr_id and r.pr_id in findings_by_pr:
                count, severity = findings_by_pr[r.pr_id]
                t.findings_count = count
                t.max_severity = severity  # type: ignore[assignment]
            out.append(t)

        if filter.sort == "findings_count":
            out.sort(key=lambda t: t.findings_count, reverse=True)
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
        repo_external_id = row.repo_external_id
        pr_id = row.pr_id
        await audit_for_ticket(
            ticket_id,
            "ticket.status_changed",
            _TicketStatusChangedPayload(from_status=prev, to_status=new_status, reason=reason),
            actor=Actor.system(),
            org_id=org_id,
            session=s,
        )
        await s.commit()
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
