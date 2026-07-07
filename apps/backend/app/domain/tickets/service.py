"""Ticket aggregate — yaaos's unit of work."""

from __future__ import annotations

import re
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
from app.domain.tickets.pull_request import PullRequest, PullRequestNotFoundError
from app.domain.tickets.pull_request import get as get_pull_request
from app.domain.tickets.pull_request import list_by_ids as list_prs_by_ids

# Six-state ticket vocabulary. `running` is set when the first workflow step dispatches;
# `hitl` and `failed` are populated by the workflow-state projection;
# `done`/`cancelled` are terminal.
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
    # Soft ref to the pipeline_runs row currently driving this ticket — the
    # run-engine's equivalent of current_workflow_execution_id above.
    current_run_id: UUID | None = None
    # Per-ticket work branch. Nullable — the old pr_review_v1 path never
    # populates it; the run engine reads it to build an action stage's
    # ActionContext.branch_name.
    branch_name: str | None = None
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
            current_workflow_execution_id=row.current_workflow_execution_id,
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
    # additions — see .
    q: str | None = None
    sort: TicketSort = "updated_desc"
    cursor: str | None = None


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
            current_workflow_execution_id=None,
            branch_name=branch_name,
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
    and workflow terminal outcomes) route through here. Looks up the title
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
    no commits. reviewer commands read admission signals (`is_draft`,
    `is_fork`, `labels`, etc.) from here without re-fetching from GitHub."""
    row = (await session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one_or_none()
    if row is None:
        raise TicketNotFoundError(str(ticket_id))
    return dict(row.payload or {})


class TicketWorkflowContext:
    """Minimal ticket fields needed to build a TicketSnapshot for workflow start.

    A plain value object (not a Pydantic model) — callers access attributes
    directly. Returned by `get_workspace_ticket_context` for use in
    `domain/reviewer.start_pr_review` which builds the typed `TicketSnapshot`
    from these fields.
    """

    __slots__ = ("org_id", "payload", "plugin_id", "pr_id", "repo_external_id")

    def __init__(
        self,
        *,
        org_id: UUID,
        plugin_id: str,
        repo_external_id: str,
        payload: dict,  # type: ignore[type-arg]
        pr_id: UUID | None = None,
    ) -> None:
        self.org_id = org_id
        self.plugin_id = plugin_id
        self.repo_external_id = repo_external_id
        self.payload = payload
        self.pr_id = pr_id


async def get_workspace_ticket_context(ticket_id: UUID) -> TicketWorkflowContext | None:
    """Read the ticket fields needed to build a `TicketSnapshot` for workflow start.

    Returns `None` when the ticket is missing. Owns its session (read-only, no
    commits). Called by `domain/reviewer.start_pr_review` which constructs the
    typed `TicketSnapshot` from the returned fields.
    """
    async with db_session() as s:
        row = (await s.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one_or_none()
    if row is None:
        return None
    return TicketWorkflowContext(
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


async def set_current_run(ticket_id: UUID, run_id: UUID, *, session: AsyncSession) -> None:
    """Stamp the pipeline run now driving this ticket. The run-engine
    equivalent of `set_workflow_execution` above. Caller commits."""
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

    Run-engine equivalent of `transition_on_workflow_start` above, keyed on
    `current_run_id` instead of `current_workflow_execution_id`. Called
    directly by `domain/pipelines`' engine (a plain acyclic import) — there
    is no `on_start`-hook indirection the way the workflow-era callback
    needed, since the run engine calls tickets functions itself.

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
    if row.status != "pending":
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
    owns it and it is not already terminal. Run-engine equivalent of
    `transition_on_workflow_terminal` above. Shape-a: takes the caller's
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
    audit row's payload — caller-supplied so the sweep / workflow / HITL
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


async def transition_on_workflow_start(
    ticket_id: UUID,
    *,
    org_id: UUID,
    workflow_execution_id: UUID,
    session: AsyncSession,
) -> bool:
    """Flip ticket pending→running when its workflow bootstraps.

    Atomic with the workflow's RUNNING state write — called from the start
    hook inside the engine's bootstrap-commit transaction.

    Returns True if flipped. Returns False (silent no-op) when:
    - the ticket is not found,
    - the ticket is owned by a different workflow execution,
    - the ticket is not currently in `pending` (re-bootstrap or already past).

    Caller commits.
    """
    row = (
        await session.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.current_workflow_execution_id != workflow_execution_id:
        return False
    if row.status != "pending":
        return False
    await _apply_transition(
        session,
        row,
        new_status="running",
        reason=None,
        org_id=org_id,
    )
    return True


async def transition_on_workflow_terminal(
    ticket_id: UUID,
    *,
    org_id: UUID,
    workflow_execution_id: UUID,
    to_status: TicketStatus,
    reason: str | None,
    session: AsyncSession,
) -> bool:
    """Flip a ticket to a terminal status only when the calling workflow
    execution still owns it and it is not already terminal. Shape-a:
    takes the caller's session, never commits.

    Returns True when the transition was applied; False on any guard miss:
    - ticket not found or wrong org
    - `current_workflow_execution_id` does not match `workflow_execution_id`
      (a newer execution has superseded this one)
    - ticket is already in a terminal state (`done`, `cancelled`, `failed`)

    Never raises on guard misses — the caller (a workflow terminal hook)
    shares the engine's transaction; raising would roll back the workflow commit.
    """
    row = (
        await session.execute(select(TicketRow).where(TicketRow.id == ticket_id, TicketRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.current_workflow_execution_id != workflow_execution_id:
        return False
    if row.status in ("done", "cancelled", "failed"):
        return False
    await _apply_transition(session, row, new_status=to_status, reason=reason, org_id=org_id)
    return True
