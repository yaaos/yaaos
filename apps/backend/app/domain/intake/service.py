"""Inbound VCS-event router."""

from __future__ import annotations

from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import select

from app.core.audit_log import audit_for_ticket, audit_for_webhook_event
from app.core.database import session as db_session
from app.core.primitives import Actor
from app.domain import pull_requests, tickets, vcs
from app.domain.intake.parsing import parse_rereview
from app.domain.vcs import (
    CommentCreated,
    PullRequestClosed,
    PullRequestReadyForReview,
    PullRequestReopened,
    PullRequestSynchronized,
    ReactionAdded,
    VCSEvent,
    VCSPullRequest,
)

log = structlog.get_logger("intake")

YAAOS_BOT_LOGIN = "yaaos[bot]"


class IntakeError(Exception):
    pass


# Audit payloads


class _WebhookFilteredPayload(BaseModel):
    reason: str
    event_kind: str
    source_event_id: str


class _RereviewRequestedPayload(BaseModel):
    comment_external_id: str


class _ReactionReceivedPayload(BaseModel):
    reaction: str
    target_comment_external_id: str


class _WebhookFailedPayload(BaseModel):
    event_kind: str
    source_event_id: str
    exception_type: str
    message: str


# ── Entry points ─────────────────────────────────────────────────────────────


async def handle_vcs_events(events: list[VCSEvent], *, org_id: UUID) -> None:
    for event in events:
        try:
            await _dispatch_one(event, org_id=org_id)
        except Exception as e:
            log.exception(
                "intake.event_failed",
                event_kind=event.kind,
                source_event_id=event.source_event_id,
            )
            await _audit_event_failed(event, e, org_id=org_id)


async def _audit_event_failed(event: VCSEvent, e: Exception, *, org_id: UUID) -> None:
    # We don't have the webhook row id at this layer — use a synthetic UUID.
    from uuid import uuid4  # noqa: PLC0415

    await audit_for_webhook_event(
        uuid4(),
        "webhook_event.failed",
        _WebhookFailedPayload(
            event_kind=event.kind,
            source_event_id=event.source_event_id,
            exception_type=type(e).__name__,
            message=str(e),
        ),
        actor=Actor.system(),
        org_id=org_id,
    )


async def _dispatch_one(event: VCSEvent, *, org_id: UUID) -> None:
    if isinstance(event, PullRequestReadyForReview):
        await _handle_pr_ready_for_review(event, org_id=org_id)
    elif isinstance(event, PullRequestSynchronized):
        await _handle_pr_synchronized(event, org_id=org_id)
    elif isinstance(event, PullRequestClosed):
        await _handle_pr_closed(event, org_id=org_id)
    elif isinstance(event, PullRequestReopened):
        await _handle_pr_reopened(event, org_id=org_id)
    elif isinstance(event, CommentCreated):
        await _handle_comment_created(event, org_id=org_id)
    elif isinstance(event, ReactionAdded):
        await _handle_reaction_added(event, org_id=org_id)


# ── Handlers ─────────────────────────────────────────────────────────────────


async def _filter_audit(event: VCSEvent, reason: str, *, org_id: UUID) -> None:
    from uuid import uuid4  # noqa: PLC0415

    await audit_for_webhook_event(
        uuid4(),
        "webhook_event.filtered",
        _WebhookFilteredPayload(
            reason=reason,
            event_kind=event.kind,
            source_event_id=event.source_event_id,
        ),
        actor=Actor.system(),
        org_id=org_id,
    )


async def _handle_pr_ready_for_review(event: PullRequestReadyForReview, *, org_id: UUID) -> None:
    # Repo-allowlist gate dropped 2026-05-16: the GitHub App install picks the
    # access scope, so any webhook we receive is already authorized.
    if event.pr.is_fork:
        await _filter_audit(event, "fork", org_id=org_id)
        return
    if event.pr.author_type == "bot":
        await _filter_audit(event, "bot_author", org_id=org_id)
        return
    pr = await refresh_pr_metadata(event.repo_external_id, event.pr, org_id=org_id)
    # Schedule a review for this PR's ticket.
    from app.domain import reviewer  # noqa: PLC0415

    ticket = await tickets.get_by_pr(pr.id, org_id=org_id)
    if ticket is None:
        return
    await reviewer.schedule_review(
        ticket_id=ticket.id,
        trigger_reason="pr_ready",
        actor=Actor.system(),
        org_id=org_id,
    )


async def _handle_pr_synchronized(event: PullRequestSynchronized, *, org_id: UUID) -> None:
    if event.pr_external_id is None:
        return
    pr = await pull_requests.get_by_external(event.plugin_id, event.pr_external_id, org_id=org_id)
    if pr is None:
        log.warning(
            "intake.pr_synchronized_unknown_pr",
            pr_external_id=event.pr_external_id,
        )
        return
    fresh = await refresh_pr_metadata_by_id(pr.repo_external_id, event.pr_external_id, org_id=org_id)
    from app.domain import reviewer  # noqa: PLC0415

    ticket = await tickets.get_by_pr(fresh.id, org_id=org_id)
    if ticket is None:
        return
    await reviewer.schedule_review(
        ticket_id=ticket.id,
        trigger_reason="pr_synchronized",
        actor=Actor.system(),
        org_id=org_id,
    )


async def _handle_pr_closed(event: PullRequestClosed, *, org_id: UUID) -> None:
    if event.pr_external_id is None:
        return
    pr = await pull_requests.get_by_external(event.plugin_id, event.pr_external_id, org_id=org_id)
    if pr is None:
        return
    new_state = "merged" if event.merged else "closed"
    await pull_requests.update_state(pr.id, new_state, org_id=org_id)  # type: ignore[arg-type]
    ticket = await tickets.get_by_pr(pr.id, org_id=org_id)
    if ticket and ticket.status == "in_review":
        await tickets.complete(ticket.id, org_id=org_id)
        from app.domain import reviewer  # noqa: PLC0415

        await reviewer.cancel_pending(ticket.id, actor=Actor.system(), org_id=org_id)


async def _handle_pr_reopened(event: PullRequestReopened, *, org_id: UUID) -> None:
    if event.pr_external_id is None:
        return
    pr = await pull_requests.get_by_external(event.plugin_id, event.pr_external_id, org_id=org_id)
    if pr is None:
        return
    await pull_requests.update_state(pr.id, "open", org_id=org_id)


async def _handle_comment_created(event: CommentCreated, *, org_id: UUID) -> None:
    if event.author_login == YAAOS_BOT_LOGIN or event.author_type == "bot":
        return
    if event.pr_external_id is None:
        return
    pr = await pull_requests.get_by_external(event.plugin_id, event.pr_external_id, org_id=org_id)
    if pr is None:
        return
    ticket = await tickets.get_by_pr(pr.id, org_id=org_id)
    if ticket is None:
        return

    matched, _agent = parse_rereview(event.body)
    if matched:
        await audit_for_ticket(
            ticket.id,
            "ticket.rereview_requested",
            _RereviewRequestedPayload(
                comment_external_id=event.comment_external_id,
            ),
            actor=Actor.github_user(event.author_login),
            org_id=org_id,
        )
        from app.domain import reviewer  # noqa: PLC0415

        await reviewer.schedule_review(
            ticket_id=ticket.id,
            trigger_reason="rereview_command",
            actor=Actor.github_user(event.author_login),
            org_id=org_id,
        )
        return

    # Inline replies to yaaos comments are deferred. The future review_comments
    # table will own that lifecycle. For now we silently drop them.


async def _handle_reaction_added(event: ReactionAdded, *, org_id: UUID) -> None:
    from app.domain.reviewer.models import PostedCommentRow  # noqa: PLC0415

    async with db_session() as s:
        posted = (
            await s.execute(
                select(PostedCommentRow).where(
                    PostedCommentRow.external_comment_id == event.target_comment_external_id
                )
            )
        ).scalar_one_or_none()
    if posted is None:
        return
    ticket = await tickets.get_by_pr(posted.pr_id, org_id=org_id)
    if ticket is None:
        return
    await audit_for_ticket(
        ticket.id,
        "ticket.reaction_received",
        _ReactionReceivedPayload(
            reaction=event.reaction,
            target_comment_external_id=event.target_comment_external_id,
        ),
        actor=Actor.github_user(event.actor_login),
        org_id=org_id,
    )


# ── PR metadata sync helpers ─────────────────────────────────────────────────


async def refresh_pr_metadata(
    repo_external_id: str, pr: VCSPullRequest, *, org_id: UUID
) -> pull_requests.PullRequest:
    """Upsert pull_requests row + ensure a ticket exists. Returns the PR row.

    Two-step: create the ticket lazily on first insert (we need pr.id to set FK on
    ticket; we also need ticket.id to set FK on pr — chicken-and-egg). Approach:
      1. Check if PR row exists. If yes, update it.
      2. If not, create ticket FIRST, then insert the PR row pointing at the
         ticket, then set ticket.pr_id to the new pr.id.
    """
    existing = await pull_requests.get_by_external(pr.plugin_id, pr.external_id, org_id=org_id)
    if existing is not None:
        upserted = await pull_requests.upsert(pr, org_id=org_id)
        ticket = await tickets.get(existing.ticket_id, org_id=org_id)
        await _sync_ticket_titles(ticket.id, pr.title, pr.body, org_id=org_id)
        return upserted

    from uuid import uuid4  # noqa: PLC0415

    from app.domain.tickets.models import TicketRow  # noqa: PLC0415

    async with db_session() as s:
        ticket_row = TicketRow(
            id=uuid4(),
            org_id=org_id,
            source="github_pr",
            source_external_id=pr.external_id,
            title=pr.title,
            description=pr.body,
            status="in_review",
            plugin_id=pr.plugin_id,
            repo_external_id=repo_external_id,
            pr_id=None,
        )
        s.add(ticket_row)
        await s.commit()
        await s.refresh(ticket_row)
        ticket_id = ticket_row.id

    upserted = await pull_requests.upsert(pr, ticket_id=ticket_id, org_id=org_id)

    async with db_session() as s:
        from sqlalchemy import update as sql_update  # noqa: PLC0415

        await s.execute(sql_update(TicketRow).where(TicketRow.id == ticket_id).values(pr_id=upserted.id))
        await s.commit()

    await audit_for_ticket(
        ticket_id,
        "ticket.created",
        _TicketCreatedAuditPayload(pr_id=upserted.id, repo_external_id=repo_external_id),
        actor=Actor.system(),
        org_id=org_id,
    )
    from app.core.events import publish  # noqa: PLC0415
    from app.domain.tickets import TicketStatusChanged  # noqa: PLC0415

    await publish(
        TicketStatusChanged(
            ticket_id=ticket_id,
            repo_external_id=repo_external_id,
            pr_id=upserted.id,
            previous_status=None,
            new_status="in_review",
        )
    )

    return upserted


async def refresh_pr_metadata_by_id(
    repo_external_id: str, pr_external_id: str, *, org_id: UUID
) -> pull_requests.PullRequest:
    """Catch-up path: fetch fresh PR from VCS, then delegate."""
    pr = await vcs.get_plugin("github").fetch_pr(pr_external_id)
    return await refresh_pr_metadata(repo_external_id, pr, org_id=org_id)


class _TicketCreatedAuditPayload(BaseModel):
    pr_id: UUID
    repo_external_id: str


async def _sync_ticket_titles(ticket_id: UUID, title: str, body: str | None, *, org_id: UUID) -> None:
    """Update ticket title/description in place (no audit — metadata sync, not a transition)."""
    from sqlalchemy import update as sql_update  # noqa: PLC0415

    from app.domain.tickets.models import TicketRow  # noqa: PLC0415

    async with db_session() as s:
        await s.execute(
            sql_update(TicketRow)
            .where(TicketRow.id == ticket_id, TicketRow.org_id == org_id)
            .values(title=title, description=body)
        )
        await s.commit()
