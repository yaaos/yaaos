"""Ticket aggregate — yaaos's unit of work."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid7

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
from app.core.vcs import resolve_plugin_id_for_repo
from app.domain.tickets.models import TicketRow
from app.domain.tickets.notifications import build_status_change_specs
from app.domain.tickets.pull_request import PullRequest, PullRequestNotFoundError
from app.domain.tickets.pull_request import get as get_pull_request
from app.domain.tickets.pull_request import list_by_ids as list_prs_by_ids

# Six-state ticket vocabulary. `running` is set when the first pipeline stage dispatches;
# `hitl` and `failed` are populated by the run-state projection;
# `done`/`cancelled` are terminal.
TicketStatus = Literal["pending", "running", "hitl", "done", "failed", "cancelled"]

# Transient INSERT-time value for `branch_name` (NOT NULL) when the real
# name needs the server-minted `ticket_id` to compute (`mint_branch_name`).
# Overwritten by an UPDATE in the same uncommitted transaction — never
# durable, never read.
_PENDING_BRANCH_NAME_PLACEHOLDER = "yaaos/pending"


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
    # Soft ref to the pipeline_runs row currently driving this ticket.
    current_run_id: UUID | None = None
    # Per-ticket work branch. The run engine reads it to build an action
    # stage's ActionContext.branch_name.
    branch_name: str
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
    max_severity: Literal["blocker", "should_fix", "nit"] | None = None
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
            current_run_id=row.current_run_id,
            branch_name=row.branch_name,
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
    q: str | None = None
    sort: TicketSort = "updated_desc"
    cursor: str | None = None
    # Filter by exact branch name — used by manual-kickoff flow to look up an
    # existing branch before creating a new ticket.
    branch_name: str | None = None


class TicketNotFoundError(LookupError):
    pass


class InvalidTicketTransition(ValueError):
    pass


class _TicketCreatedPayload(BaseModel):
    """Unified audit payload for the ticket.created kind.

    Written by create_from_pr (and any future create_from_<source>) at the
    moment of first insert. Source-specific facts (pr_id) are written via the
    separate ticket.pr_bound kind when attach_pr_to_ticket runs.
    """

    type: str
    source: str
    source_external_id: str
    idempotency_key: str


class _TicketPrBoundPayload(BaseModel):
    """Audit payload for the ticket.pr_bound kind.

    Written by attach_pr_to_ticket when it successfully back-fills pr_id.
    """

    pr_id: UUID
    repo_external_id: str


class _TicketStatusChangedPayload(BaseModel):
    from_status: str
    to_status: str
    reason: str | None = None


async def _insert_ticket_atomic(
    *,
    org_id: UUID,
    type: str,
    source: str,
    source_external_id: str,
    title: str,
    description: str | None,
    repo_external_id: str,
    plugin_id: str,
    idempotency_key: str,
    payload: dict,
    status: str,
    conflict_target: tuple[str, ...],
    session: AsyncSession,
    branch_name: str | None = None,
) -> tuple[UUID, bool]:
    """Race-safe ticket INSERT.

    Uses INSERT … ON CONFLICT DO NOTHING on `conflict_target`. On conflict
    (race loser), re-SELECTs by `conflict_target + org_id` to recover the
    winner's id — never returns None. Returns `(ticket_id, created)`.
    Caller commits; never commits here.
    """
    # `branch_name` is NOT NULL at the DB level, but the caller's mint of
    # `mint_branch_name(title, ticket_id)` needs `ticket_id`, which is only
    # known after this INSERT returns (server-minted UUIDv7). Insert a
    # placeholder when the caller hasn't supplied one; the caller then
    # overwrites it with the real minted value in the same uncommitted
    # transaction — no reader ever observes the placeholder.
    stmt = (
        pg_insert(TicketRow)
        .values(
            org_id=org_id,
            source=source,
            source_external_id=source_external_id,
            title=title,
            description=description,
            status=status,
            plugin_id=plugin_id,
            repo_external_id=repo_external_id,
            pr_id=None,
            type=type,
            idempotency_key=idempotency_key,
            payload=payload,
            branch_name=branch_name if branch_name is not None else _PENDING_BRANCH_NAME_PLACEHOLDER,
        )
        .on_conflict_do_nothing(index_elements=list(conflict_target))
        .returning(TicketRow.id)
    )
    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    if inserted_id is not None:
        return inserted_id, True

    # Race loser: re-SELECT to recover the winner's id.
    conditions = [TicketRow.org_id == org_id]
    col_map = {
        "source": TicketRow.source,
        "source_external_id": TicketRow.source_external_id,
        "idempotency_key": TicketRow.idempotency_key,
    }
    local_vals = {
        "source": source,
        "source_external_id": source_external_id,
        "idempotency_key": idempotency_key,
    }
    for col_name in conflict_target:
        if col_name == "org_id":
            continue
        col = col_map.get(col_name)
        val = local_vals.get(col_name)
        if col is not None and val is not None:
            conditions.append(col == val)
    existing_id = (await session.execute(select(TicketRow.id).where(*conditions))).scalar_one()
    return existing_id, False


async def create_from_pr(
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
    branch_name: str | None = None,
) -> tuple[UUID, bool]:
    """Race-safe ticket INSERT for a GitHub PR-opened-style event.

    Establishes the `create_from_<source>` convention for intake-source
    ticket constructors. `branch_name` is intake-supplied when the caller
    already knows the work branch (a PR ticket's own head branch); when
    omitted, a fresh ticket mints one deterministically via
    `mint_branch_name(title, ticket_id)` — the ticket id is only known after
    insert, so minting happens post-insert on the winning row only (a race
    loser's branch_name was already set by the winner). On `created=True`
    writes the `ticket.created` audit row and fires `notify_ticket_status_change`.
    On conflict (race loser) returns `(winner_id, False)` — the caller should
    exit without doing further work. Caller commits; never commits here.
    """
    ticket_id, created = await _insert_ticket_atomic(
        org_id=org_id,
        type="github_pr",
        source="github_pr",
        source_external_id=source_external_id,
        title=title,
        description=description,
        repo_external_id=repo_external_id,
        plugin_id=plugin_id,
        idempotency_key=idempotency_key,
        payload=payload,
        status="pending",
        conflict_target=("org_id", "source", "source_external_id"),
        session=session,
        branch_name=branch_name,
    )
    if created:
        if branch_name is None:
            minted_branch_name = mint_branch_name(title, ticket_id)
            await session.execute(
                update(TicketRow).where(TicketRow.id == ticket_id).values(branch_name=minted_branch_name)
            )
        await audit_for_ticket(
            ticket_id,
            "ticket.created",
            _TicketCreatedPayload(
                type="github_pr",
                source="github_pr",
                source_external_id=source_external_id,
                idempotency_key=idempotency_key,
            ),
            actor=Actor.system(),
            org_id=org_id,
            session=session,
        )
        await notify_ticket_status_change(
            ticket_id=ticket_id,
            org_id=org_id,
            new_status="pending",
            previous_status=None,
            session=session,
        )
    return ticket_id, created


async def create_from_schedule(
    *,
    org_id: UUID,
    source_external_id: str,
    title: str,
    repo_external_id: str,
    plugin_id: str,
    session: AsyncSession,
) -> tuple[UUID, bool]:
    """Race-safe ticket INSERT for a schedule firing — the second instance of
    the `create_from_<source>` convention `create_from_pr` establishes.

    Schedule tickets are yaaos-authored (no upstream branch to inherit), so
    `branch_name` is always freshly minted here (unlike `create_from_pr`,
    which accepts an intake-supplied branch for PR tickets). On
    `created=True` writes the `ticket.created` audit row and fires
    `notify_ticket_status_change`. On conflict (a redelivered
    `source_external_id` — the `(org_id, source, source_external_id)` unique
    constraint) returns the existing ticket's id with `created=False`; the
    caller does no further work.
    """
    ticket_id, created = await _insert_ticket_atomic(
        org_id=org_id,
        type="schedule",
        source="schedule",
        source_external_id=source_external_id,
        title=title,
        description=None,
        repo_external_id=repo_external_id,
        plugin_id=plugin_id,
        idempotency_key=source_external_id,
        payload={},
        status="pending",
        conflict_target=("org_id", "source", "source_external_id"),
        session=session,
    )
    if created:
        minted_branch_name = mint_branch_name(title, ticket_id)
        await session.execute(
            update(TicketRow).where(TicketRow.id == ticket_id).values(branch_name=minted_branch_name)
        )
        await audit_for_ticket(
            ticket_id,
            "ticket.created",
            _TicketCreatedPayload(
                type="schedule",
                source="schedule",
                source_external_id=source_external_id,
                idempotency_key=source_external_id,
            ),
            actor=Actor.system(),
            org_id=org_id,
            session=session,
        )
        await notify_ticket_status_change(
            ticket_id=ticket_id,
            org_id=org_id,
            new_status="pending",
            previous_status=None,
            session=session,
        )
    return ticket_id, created


async def create_from_manual(
    *,
    org_id: UUID,
    title: str,
    repo_external_id: str,
    actor: Actor,
    session: AsyncSession,
    branch_name: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[UUID, bool]:
    """Race-safe ticket INSERT for a user-initiated manual task.

    Each call with `idempotency_key=None` creates a distinct ticket — a fresh
    `uuid7()` is minted as `source_external_id` so there is no collision
    target. Supplying an `idempotency_key` makes repeated calls idempotent:
    the second call returns `(winner_id, False)` without further work.
    When `branch_name` is omitted, one is minted deterministically via
    `mint_branch_name(title, ticket_id)`. On `created=True` writes the
    `ticket.created` audit row and fires `notify_ticket_status_change`.
    Caller commits; never commits here.
    """
    key = idempotency_key if idempotency_key is not None else str(uuid7())
    plugin_id = await resolve_plugin_id_for_repo(org_id, repo_external_id)
    ticket_id, created = await _insert_ticket_atomic(
        org_id=org_id,
        type="manual",
        source="manual",
        source_external_id=key,
        title=title,
        description=None,
        repo_external_id=repo_external_id,
        plugin_id=plugin_id,
        idempotency_key=key,
        payload={},
        status="pending",
        conflict_target=("org_id", "source", "source_external_id"),
        session=session,
        branch_name=branch_name,
    )
    if created:
        if branch_name is None:
            minted_branch_name = mint_branch_name(title, ticket_id)
            await session.execute(
                update(TicketRow).where(TicketRow.id == ticket_id).values(branch_name=minted_branch_name)
            )
        await audit_for_ticket(
            ticket_id,
            "ticket.created",
            _TicketCreatedPayload(
                type="manual",
                source="manual",
                source_external_id=key,
                idempotency_key=key,
            ),
            actor=actor,
            org_id=org_id,
            session=session,
        )
        await notify_ticket_status_change(
            ticket_id=ticket_id,
            org_id=org_id,
            new_status="pending",
            previous_status=None,
            session=session,
        )
    return ticket_id, created


async def get_by_branch(branch_name: str, *, org_id: UUID, session: AsyncSession) -> Ticket | None:
    """Return the most recently created ticket on `branch_name` in `org_id`, or None.

    Shape (a): caller supplies the session so this composes inside a larger
    transaction. When multiple tickets share the same branch (a manual
    ticket re-using an existing branch, or a test suite seeding several),
    the newest row wins so callers always see the latest work.
    """
    row = (
        await session.execute(
            select(TicketRow)
            .where(TicketRow.branch_name == branch_name, TicketRow.org_id == org_id)
            .order_by(TicketRow.created_at.desc(), TicketRow.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return Ticket.from_row(row) if row is not None else None


async def notify_ticket_status_change(
    *,
    ticket_id: UUID,
    org_id: UUID,
    new_status: str,
    previous_status: str | None,
    session: AsyncSession,
) -> None:
    """Single broadcast seam for TICKET_STATUS_CHANGED events.

    All ticket status changes (creation at pending, state transitions,
    and run terminal outcomes) route through here. Looks up the title
    internally so callers stay terse. Caller commits.
    """
    row = (
        await session.execute(
            select(TicketRow.title).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id)
        )
    ).first()
    ticket_title = row[0] if row is not None else ""

    members = await list_active_member_ids(session, org_id)
    publish_general_after_commit(
        session,
        org_id=org_id,
        kind=GeneralEventKind.TICKET_STATUS_CHANGED,
        payload={
            "ticket_id": str(ticket_id),
            "new_status": new_status,
            "previous_status": previous_status,
        },
    )
    specs = build_status_change_specs(
        ticket_id=ticket_id,
        org_id=org_id,
        ticket_title=ticket_title,
        member_user_ids=members,
        new_status=new_status,
    )
    if specs:
        await enqueue(
            fanout,
            args={"specs": [s.to_dict() for s in specs]},
            session=session,
        )


async def attach_pr_to_ticket(
    ticket_id: UUID,
    *,
    org_id: UUID,
    pr_id: UUID,
    session: AsyncSession,
) -> None:
    """Back-fill `pr_id` on a ticket and write the `ticket.pr_bound` audit row.

    The `WHERE pr_id IS NULL AND org_id = :org_id` guard makes the op
    idempotent: if a concurrent caller already set `pr_id`, this is a
    safe no-op with no audit row written. Caller commits.
    """
    result = await session.execute(
        update(TicketRow)
        .where(
            TicketRow.id == ticket_id,
            TicketRow.org_id == org_id,
            TicketRow.pr_id.is_(None),
        )
        .values(pr_id=pr_id)
        .returning(TicketRow.repo_external_id)
    )
    row = result.first()
    if row is not None:
        repo_external_id = row[0]
        await audit_for_ticket(
            ticket_id,
            "ticket.pr_bound",
            _TicketPrBoundPayload(pr_id=pr_id, repo_external_id=repo_external_id or ""),
            actor=Actor.system(),
            org_id=org_id,
            session=session,
        )


async def get(ticket_id: UUID, *, org_id: UUID) -> Ticket:
    async with db_session() as s:
        row = (
            await s.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        t = Ticket.from_row(row)
    if row.pr_id is not None:
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
    no commits. Callers read admission signals (`is_draft`, `is_fork`,
    `labels`, etc.) from here without re-fetching from GitHub."""
    row = (await session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one_or_none()
    if row is None:
        raise TicketNotFoundError(str(ticket_id))
    return dict(row.payload or {})


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

    Called by `domain/findings` after each finding report or verdict.
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
    — `domain/findings` writes the rollup after each finding report or verdict.
    """
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
        if filter.branch_name:
            stmt = stmt.where(TicketRow.branch_name == filter.branch_name)

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


async def set_current_run(ticket_id: UUID, run_id: UUID, *, session: AsyncSession) -> None:
    """Stamp the pipeline run now driving this ticket. Caller commits."""
    await session.execute(update(TicketRow).where(TicketRow.id == ticket_id).values(current_run_id=run_id))


_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


def mint_branch_name(title: str, ticket_id: UUID) -> str:
    """Pure: `yaaos/<slugified-title ~40ch>-<uuid7[:8]>`, falling back to
    `yaaos/ticket-<uuid7[:8]>` when the title yields no usable slug (empty,
    or all non-alphanumeric characters). The suffix is the ticket's own id
    (already a uuid7) rather than a freshly-minted one, so calling this
    twice for the same ticket is idempotent."""
    shortid = ticket_id.hex[:8]
    slug = _SLUG_INVALID.sub("-", title.lower()).strip("-")[:40].strip("-")
    if not slug:
        return f"yaaos/ticket-{shortid}"
    return f"yaaos/{slug}-{shortid}"


async def transition_ticket_on_run_start(
    ticket_id: UUID,
    *,
    org_id: UUID,
    run_id: UUID,
    session: AsyncSession,
) -> bool:
    """Flip ticket pending→running when its pipeline run starts running.

    Called directly by `domain/pipelines`' engine (a plain acyclic import) —
    no hook indirection; the run engine calls tickets functions itself.

    Returns True if flipped. Returns False (silent no-op) when:
    - the ticket is not found,
    - the ticket is owned by a different run,
    - the ticket is not currently in `pending`.

    Caller commits.
    """
    row = (
        await session.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.current_run_id != run_id:
        return False
    # Allow "cancelled" as a valid from-state: a kill+replace flow leaves the
    # ticket "cancelled" after the kill; the replacement run's promotion should
    # flip it back to "running".
    if row.status not in ("pending", "cancelled"):
        return False
    await _apply_transition(
        session,
        row,
        new_status="running",
        reason=None,
        org_id=org_id,
    )
    return True


async def transition_ticket_on_run_terminal(
    ticket_id: UUID,
    *,
    org_id: UUID,
    run_id: UUID,
    to_status: TicketStatus,
    reason: str | None,
    session: AsyncSession,
) -> bool:
    """Flip a ticket to a terminal status only when the calling run still
    owns it and it is not already terminal. Shape-a: takes the caller's
    session, never commits.

    Returns True when the transition was applied; False on any guard miss:
    - ticket not found or wrong org
    - `current_run_id` does not match `run_id` (a newer run has superseded
      this one)
    - ticket is already in a terminal state (`done`, `cancelled`, `failed`)

    Never raises on guard misses — the caller (the run engine's terminal
    handling) shares the transaction; raising would roll back the run's
    terminal commit.
    """
    row = (
        await session.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.current_run_id != run_id:
        return False
    if row.status in ("done", "cancelled", "failed"):
        return False
    await _apply_transition(session, row, new_status=to_status, reason=reason, org_id=org_id)
    return True


async def transition_ticket_on_run_paused(
    ticket_id: UUID,
    *,
    org_id: UUID,
    run_id: UUID,
    session: AsyncSession,
) -> bool:
    """Flip a ticket to `hitl` when a boundary pause traps its owning run.

    Same ownership + not-already-terminal guard as
    `transition_ticket_on_run_terminal` — a pause is not itself a terminal
    run state, so it gets its own entry point rather than overloading that
    function's terminal-only contract. Caller commits.
    """
    row = (
        await session.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.current_run_id != run_id:
        return False
    if row.status in ("done", "cancelled", "failed"):
        return False
    await _apply_transition(session, row, new_status="hitl", reason=None, org_id=org_id)
    return True


async def transition_ticket_on_run_resumed(
    ticket_id: UUID,
    *,
    org_id: UUID,
    run_id: UUID,
    session: AsyncSession,
) -> bool:
    """Flip a ticket back to `running` when a pause on its owning run
    resolves with `approve`. Only applies from `hitl` — a ticket that moved
    on for any other reason is left alone. Caller commits.
    """
    row = (
        await session.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.current_run_id != run_id:
        return False
    if row.status != "hitl":
        return False
    await _apply_transition(session, row, new_status="running", reason=None, org_id=org_id)
    return True


async def complete(ticket_id: UUID, *, org_id: UUID) -> None:
    await _transition(ticket_id, new_status="done", org_id=org_id)


async def abandon(ticket_id: UUID, *, reason: str, org_id: UUID) -> None:
    await _transition(ticket_id, new_status="cancelled", org_id=org_id, reason=reason)


async def fail(ticket_id: UUID, *, reason: str, org_id: UUID) -> None:
    """Move a `running` ticket to `failed`. The reason is recorded in the
    audit row's payload — caller-supplied so the sweep / run / HITL
    layers can each tag their own failure mode."""
    await _transition(ticket_id, new_status="failed", org_id=org_id, reason=reason)


async def _apply_transition(
    s: AsyncSession,
    row: TicketRow,
    *,
    new_status: TicketStatus,
    reason: str | None,
    org_id: UUID,
) -> None:
    """Apply a status transition on an already-loaded row, within the caller's
    session. Fires audit, SSE, and notification outbox — all stashed on the
    session and flushed atomically with the caller's commit. Does not commit."""
    prev = row.status
    await s.execute(update(TicketRow).where(TicketRow.id == row.id).values(status=new_status))
    await audit_for_ticket(
        row.id,
        "ticket.status_changed",
        _TicketStatusChangedPayload(from_status=prev, to_status=new_status, reason=reason),
        actor=Actor.system(),
        org_id=org_id,
        session=s,
    )
    await notify_ticket_status_change(
        ticket_id=row.id,
        org_id=org_id,
        new_status=new_status,
        previous_status=prev,
        session=s,
    )


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
        await _apply_transition(s, row, new_status=new_status, reason=reason, org_id=org_id)
        await s.commit()
