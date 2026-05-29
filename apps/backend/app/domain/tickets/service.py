"""Ticket aggregate — yaaos's unit of work."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_ticket
from app.core.database import session as db_session
from app.core.notifications import fanout
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.tasks import enqueue
from app.core.tenancy import list_active_member_ids
from app.domain.tickets.models import TicketRow
from app.domain.tickets.notifications import build_status_change_specs

# Six-state ticket vocabulary. `pending` is the initial state set by `create()`;
# `running` is set when the first workflow step dispatches; `hitl` and `failed`
# are populated by the workflow-state projection; `done`/`cancelled` are terminal.
TicketStatus = Literal["pending", "running", "hitl", "done", "failed", "cancelled"]


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
    type: str = "pr_review"
    idempotency_key: str | None = None
    payload: dict = Field(default_factory=dict)
    current_workflow_execution_id: UUID | None = None
    # Enriched fields (denormalized at read-time from the linked PR; nullable
    # because a ticket can briefly exist without its PR row in the create flow).
    pr_number: int | None = None
    pr_html_url: str | None = None
    author_login: str | None = None
    is_draft: bool | None = None
    created_at: datetime
    updated_at: datetime
    # fields (nullable until the projections that populate them ship).
    current_stage: str | None = None
    findings_count: int = 0
    max_severity: Literal["low", "medium", "high"] | None = None
    builder_kind: Literal["user", "system"] = "user"
    builder_display_name: str | None = None

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
            type=row.type,
            idempotency_key=row.idempotency_key,
            payload=dict(row.payload or {}),
            current_workflow_execution_id=row.current_workflow_execution_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            findings_count=row.findings_count,
            max_severity=row.max_severity,  # type: ignore[arg-type]
        )


TicketSort = Literal["updated_desc", "updated_asc", "created_desc", "status", "findings_count"]


class TicketFilter(BaseModel):
    repo_external_ids: list[str] | None = None
    author_logins: list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    statuses: list[TicketStatus] | None = None
    # additions — see .
    q: str | None = None
    sort: TicketSort = "updated_desc"
    cursor: str | None = None


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
    members = await list_active_member_ids(session, org_id)
    publish_general_after_commit(
        session,
        org_id=org_id,
        kind=GeneralEventKind.TICKET_STATUS_CHANGED,
        payload={
            "ticket_id": str(row.id),
            "new_status": "pending",
            "previous_status": None,
        },
    )
    specs = build_status_change_specs(
        ticket_id=row.id,
        org_id=org_id,
        ticket_title=row.title,
        member_user_ids=members,
        new_status="pending",
    )
    if specs:
        await enqueue(
            fanout,
            args={"specs": [s.to_dict() for s in specs]},
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
            org_id=org_id,
            source="github_pr",
            source_external_id=source_external_id,
            title=title,
            description=description,
            status="running",
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
        members = await list_active_member_ids(s, org_id)
        publish_general_after_commit(
            s,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={
                "ticket_id": str(row_id),
                "new_status": "running",
                "previous_status": None,
            },
        )
        specs = build_status_change_specs(
            ticket_id=row_id,
            org_id=org_id,
            ticket_title=title,
            member_user_ids=members,
            new_status="running",
        )
        if specs:
            await enqueue(
                fanout,
                args={"specs": [s.to_dict() for s in specs]},
                session=s,
            )
        await s.commit()
    return await get(row_id, org_id=org_id)


async def get(ticket_id: UUID, *, org_id: UUID) -> Ticket:
    from app.domain.pull_requests import get as get_pull_request  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        t = Ticket.from_row(row)
    if row.pr_id is not None:
        from app.domain.pull_requests import PullRequestNotFoundError  # noqa: PLC0415

        try:
            pr = await get_pull_request(row.pr_id, org_id=row.org_id)
            t.pr_number = pr.number
            t.pr_html_url = pr.html_url
            t.author_login = pr.author_login
            t.is_draft = pr.is_draft
        except PullRequestNotFoundError:
            pass
    return t


async def get_payload(ticket_id: UUID, *, session: AsyncSession) -> dict[str, Any]:
    """Return the ticket's intake payload dict. Required session — read-only,
    no commits. reviewer commands read admission signals (`is_draft`,
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


async def update_findings_summary(
    ticket_id: UUID,
    *,
    findings_count: int,
    max_severity: str | None,
    session: AsyncSession,
) -> None:
    """Write the denormalized findings rollup onto the ticket row.

    Called by reviewer after each review run and on ack/push-back.
    Caller commits. No-op when the ticket is missing (defensive).
    """
    await session.execute(
        update(TicketRow)
        .where(TicketRow.id == ticket_id)
        .values(findings_count=findings_count, max_severity=max_severity)
    )


async def list_running_older_than(cutoff: datetime) -> list[tuple[UUID, UUID, UUID | None]]:
    """Return `(ticket_id, org_id, pr_id)` triples for every `running` ticket
    created before *cutoff*.

    Does not filter by org — intended for system sweeps that process all orgs.
    `pr_id` is ``None`` for tickets not yet linked to a PR. Callers perform any
    secondary domain checks (e.g. review-row existence) before acting.
    """
    async with db_session() as s:
        rows = (
            await s.execute(
                select(TicketRow.id, TicketRow.org_id, TicketRow.pr_id).where(
                    TicketRow.status == "running",
                    TicketRow.created_at < cutoff,
                )
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def list_tickets(
    filter: TicketFilter,
    *,
    org_id: UUID,
    limit: int = 50,
) -> list[Ticket]:
    """List tickets for the org, applying the requested filters + sort.

    `findings_count` + `max_severity` are read directly from the ticket row
    — reviewer writes the rollup after each review run and on ack/push-back.
    """
    from app.domain.pull_requests import PullRequest  # noqa: PLC0415
    from app.domain.pull_requests import list_by_ids as list_prs_by_ids  # noqa: PLC0415

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
            "findings_count": TicketRow.findings_count.desc(),
        }.get(filter.sort, TicketRow.updated_at.desc())
        stmt = stmt.order_by(sort_clause).limit(limit)

        rows = (await s.execute(stmt)).scalars().all()

    # Batch-enrich PR data via the public pull_requests op (one query, not N+1).
    pr_ids = [r.pr_id for r in rows if r.pr_id is not None]
    prs_by_id: dict[UUID, PullRequest] = {}
    if pr_ids:
        prs_by_id = {p.id: p for p in await list_prs_by_ids(pr_ids)}

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
        out.append(t)
    return out


async def upsert_ticket_for_pr(
    *,
    org_id: UUID,
    source_external_id: str,
    title: str,
    description: str | None,
    repo_external_id: str,
    plugin_id: str,
    idempotency_key: str,
    payload: dict,
    session: AsyncSession,
) -> tuple[UUID | None, bool]:
    """Race-safe ticket INSERT for a GitHub PR-opened-style event.

    Uses INSERT … ON CONFLICT DO NOTHING on `(org_id, source, source_external_id)`.
    Returns `(ticket_id, created)`.  On conflict (race loser), returns
    `(None, False)` — the caller should exit without doing further work.
    Caller commits; never commits here.
    """
    stmt = (
        pg_insert(TicketRow)
        .values(
            org_id=org_id,
            source="github_pr",
            source_external_id=source_external_id,
            title=title,
            description=description,
            status="running",
            plugin_id=plugin_id,
            repo_external_id=repo_external_id,
            pr_id=None,
            type="github_pr",
            idempotency_key=idempotency_key,
            payload=payload,
            current_workflow_execution_id=None,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "source", "source_external_id"])
        .returning(TicketRow.id)
    )
    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    if inserted_id is None:
        return None, False
    return inserted_id, True


async def attach_pr_to_ticket(
    ticket_id: UUID,
    *,
    pr_id: UUID,
    session: AsyncSession,
) -> None:
    """Back-fill `pr_id` on a ticket that was inserted without it.

    The `WHERE pr_id IS NULL` guard makes the op idempotent: if a concurrent
    caller already set `pr_id`, this is a safe no-op.  Caller commits.
    """
    await session.execute(
        update(TicketRow).where(TicketRow.id == ticket_id, TicketRow.pr_id.is_(None)).values(pr_id=pr_id)
    )


async def set_workflow_execution(
    ticket_id: UUID,
    *,
    workflow_execution_id: UUID,
    session: AsyncSession,
) -> None:
    """Stamp the workflow execution id onto the ticket after engine.start().

    Caller commits.
    """
    await session.execute(
        update(TicketRow)
        .where(TicketRow.id == ticket_id)
        .values(current_workflow_execution_id=workflow_execution_id)
    )


async def complete(ticket_id: UUID, *, org_id: UUID) -> None:
    await _transition(ticket_id, new_status="done", org_id=org_id)


async def abandon(ticket_id: UUID, *, reason: str, org_id: UUID) -> None:
    await _transition(ticket_id, new_status="cancelled", org_id=org_id, reason=reason)


async def fail(ticket_id: UUID, *, reason: str, org_id: UUID) -> None:
    """Move a `running` ticket to `failed`. The reason is recorded in the
    audit row's payload — caller-supplied so the sweep / workflow / HITL
    layers can each tag their own failure mode."""
    await _transition(ticket_id, new_status="failed", org_id=org_id, reason=reason)


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
        if row.status in ("done", "cancelled", "failed"):
            raise InvalidTicketTransition(f"ticket {ticket_id} is terminal ({row.status}); cannot transition")
        prev = row.status
        await s.execute(update(TicketRow).where(TicketRow.id == ticket_id).values(status=new_status))
        await audit_for_ticket(
            ticket_id,
            "ticket.status_changed",
            _TicketStatusChangedPayload(from_status=prev, to_status=new_status, reason=reason),
            actor=Actor.system(),
            org_id=org_id,
            session=s,
        )
        members = await list_active_member_ids(s, org_id)
        publish_general_after_commit(
            s,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={
                "ticket_id": str(ticket_id),
                "new_status": new_status,
                "previous_status": prev,
            },
        )
        specs = build_status_change_specs(
            ticket_id=ticket_id,
            org_id=org_id,
            ticket_title=row.title,
            member_user_ids=members,
            new_status=new_status,
        )
        if specs:
            await enqueue(
                fanout,
                args={"specs": [s.to_dict() for s in specs]},
                session=s,
            )
        await s.commit()
