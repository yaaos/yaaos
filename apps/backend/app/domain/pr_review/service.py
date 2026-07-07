"""Service surface for `domain/pr_review`.

`handle_pr_comment` is the entry from `plugins/github`: the `@yaaos`
grammar (`re-review` / `cancel`) is handled inline; free text is stamped
into `pr_comments` and queued for classification via `CLASSIFY_COMMENT`.

`CLASSIFY_COMMENT` runs the comment classifier (idempotent on
`classification`, so redelivery is a no-op), then either replies with a
canned clarification (`unclear` — low confidence or no finding anchor) or
hands off to `maybe_start_batch_run`. `AFTER_RUN_TERMINAL` is registered
with `domain/pipelines.register_run_terminal_hook` at import time
(`apps/backend/app/domain/pr_review/__init__.py`) so every pipeline run
reaching a terminal state re-evaluates whether a waiting comment batch
should start a run.

`evaluate_auto_approval` stays a stub — its wiring into `AFTER_RUN_TERMINAL`
lands separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.byok import get as byok_get
from app.core.database import session as db_session
from app.core.intake import parse_yaaos_command
from app.core.tasks import TaskRef, enqueue, task
from app.core.vcs import post_comment_reply
from app.domain.findings import find_by_external_comment
from app.domain.findings import get as get_finding
from app.domain.pipelines import (
    Kickoff,
    RunAlreadyTerminalError,
    has_run_in_flight,
    request_cancel,
    start_run,
)
from app.domain.pr_review.llm import ClassifyCommentInput, classify_comment
from app.domain.pr_review.models import PRCommentRow
from app.domain.pr_review.types import InboundComment, PRComment
from app.domain.repos import find_bindings
from app.domain.tickets import get as get_ticket
from app.domain.tickets import get_pull_request

# Below this confidence (0-100), a classification is treated as `unclear`
# regardless of what the LLM asserted — the comment gets a generic
# clarification reply instead of joining a batch.
_LOW_CONFIDENCE_MAX = 50

_UNCLEAR_REPLY = (
    "Not sure what you're asking here. Reply directly on a specific finding's "
    "thread, or comment `@yaaos re-review` to start a fresh review."
)

_ACCEPTANCE_REPLY = "Dismissing this finding based on your feedback."


async def _start_re_review(
    org_id: UUID, ticket_id: UUID, *, author_login: str, session: AsyncSession
) -> None:
    ticket = await get_ticket(ticket_id, org_id=org_id)
    bindings = await find_bindings(org_id, ticket.repo_external_id, "github:pr_opened", session=session)
    if not bindings:
        return
    pr_head_sha: str | None = None
    pr_base_sha: str | None = None
    if ticket.pr_id is not None:
        pr = await get_pull_request(ticket.pr_id, org_id=org_id)
        pr_head_sha = pr.head_sha
        pr_base_sha = pr.base_sha
    kickoff = Kickoff(
        intake_point_id="github:pr_opened",
        actor=Actor.github_user(author_login),
        input_text=None,
        pr_base_sha=pr_base_sha,
        pr_head_sha=pr_head_sha,
    )
    for binding in bindings:
        await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=binding.pipeline_id,
            kickoff=kickoff,
            session=session,
        )


async def _cancel_current_run(
    org_id: UUID, ticket_id: UUID, *, author_login: str, session: AsyncSession
) -> None:
    ticket = await get_ticket(ticket_id, org_id=org_id)
    if ticket.current_run_id is None:
        return
    try:
        # `request_cancel` reads org_id off the contextvar (`require_org_context`);
        # the webhook path this is called from is unauthenticated (no
        # ORG_SCOPED middleware to set it), so open the context explicitly —
        # the same convention every other non-HTTP entry point follows.
        async with org_context(org_id, ActorKind.GITHUB_USER, actor_id=None):
            await request_cancel(
                ticket.current_run_id, actor=Actor.github_user(author_login), session=session
            )
    except RunAlreadyTerminalError:
        pass


async def handle_pr_comment(
    *, org_id: UUID, ticket_id: UUID, comment: InboundComment, session: AsyncSession
) -> None:
    """Entry from the VCS plugin (bot comments filtered, PR→ticket resolved
    before the call). `@yaaos` grammar first; free text enqueues classification."""
    cmd = parse_yaaos_command(comment.body)
    if cmd == "re-review":
        await _start_re_review(org_id, ticket_id, author_login=comment.author_login, session=session)
        return
    if cmd == "cancel":
        await _cancel_current_run(org_id, ticket_id, author_login=comment.author_login, session=session)
        return

    finding_id: UUID | None = None
    if comment.in_reply_to_external_id is not None:
        anchor = await find_by_external_comment(org_id, comment.in_reply_to_external_id, session=session)
        finding_id = anchor.id if anchor is not None else None

    row = PRCommentRow(
        org_id=org_id,
        ticket_id=ticket_id,
        comment_external_id=comment.external_id,
        in_reply_to_external_id=comment.in_reply_to_external_id,
        author_login=comment.author_login,
        body=comment.body,
        finding_id=finding_id,
    )
    session.add(row)
    await session.flush()
    await enqueue(_classify_comment_task, args={"comment_id": str(row.id)}, session=session)


@dataclass(frozen=True)
class _ClassifyResult:
    classification: str
    org_id: UUID
    ticket_id: UUID
    comment_external_id: str


async def _stamp_classification(comment_id: UUID) -> _ClassifyResult | None:
    """Opens and commits its own transaction: the classification stamp must
    land (and be visible to a concurrent `maybe_start_batch_run`) before the
    canned reply is attempted — see `_classify_comment_task`'s module note on
    crash tolerance. Idempotent: `None` iff the comment is gone or already
    classified (redelivery no-op)."""
    async with db_session() as s:
        row = await s.get(PRCommentRow, comment_id)
        if row is None or row.classification is not None:
            return None
        org_id, ticket_id = row.org_id, row.ticket_id

        if row.finding_id is None:
            classification = "unclear"
        else:
            finding = await get_finding(row.finding_id, session=s)
            api_key = await byok_get(org_id, "anthropic", session=s)
            result = await classify_comment(
                ClassifyCommentInput(
                    finding_body=finding.body, finding_severity=finding.severity, comment_body=row.body
                ),
                api_key=api_key,
            )
            classification = result.intent if result.confidence >= _LOW_CONFIDENCE_MAX else "unclear"

        row.classification = classification
        comment_external_id = row.comment_external_id
        await s.commit()
        return _ClassifyResult(classification, org_id, ticket_id, comment_external_id)


async def _reply_unclear(*, org_id: UUID, ticket_id: UUID, comment_external_id: str) -> None:
    ticket = await get_ticket(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return
    pr = await get_pull_request(ticket.pr_id, org_id=org_id)
    await post_comment_reply(ticket.plugin_id, org_id, pr.external_id, comment_external_id, _UNCLEAR_REPLY)


@task("pr_review.classify_comment", queue="pipelines", max_retries=3)
async def _classify_comment_task(*, comment_id: str) -> None:
    """Idempotent (classification set → skip). Low confidence or no finding
    anchor stamps `unclear`, then replies with a canned clarification —
    stamp+commit lands FIRST, the reply is a separate step after, so a crash
    between the two loses one canned reply rather than risking a double
    reply on redelivery (the redelivered run sees `classification` already
    set and returns early). Any other classification hands off to
    `maybe_start_batch_run`."""
    result = await _stamp_classification(UUID(comment_id))
    if result is None:
        return
    if result.classification == "unclear":
        await _reply_unclear(
            org_id=result.org_id, ticket_id=result.ticket_id, comment_external_id=result.comment_external_id
        )
        return
    async with db_session() as s:
        await maybe_start_batch_run(result.org_id, result.ticket_id, session=s)
        await s.commit()


CLASSIFY_COMMENT: TaskRef = _classify_comment_task


async def _render_batch(comments: list[PRCommentRow], *, session: AsyncSession) -> str:
    lines: list[str] = ["PR comments requiring a response:"]
    for c in comments:
        anchor = ""
        if c.finding_id is not None:
            finding = await get_finding(c.finding_id, session=session)
            anchor = f" on {finding.handle}"
        lines.append(f"- [{c.classification}]{anchor} @{c.author_login}: {c.body}")
    return "\n".join(lines)


async def maybe_start_batch_run(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """No run in flight AND waiting comments exist → claim + batch + start_run."""
    if await has_run_in_flight(ticket_id, session=session):
        return

    waiting = (
        (
            await session.execute(
                select(PRCommentRow)
                .where(
                    PRCommentRow.ticket_id == ticket_id,
                    PRCommentRow.claimed_by_run_id.is_(None),
                    PRCommentRow.classification.is_not(None),
                    PRCommentRow.classification != "unclear",
                )
                .order_by(PRCommentRow.created_at)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not waiting:
        return

    ticket = await get_ticket(ticket_id, org_id=org_id)
    bindings = await find_bindings(org_id, ticket.repo_external_id, "github:pr_comment", session=session)
    if not bindings:
        return

    kickoff = Kickoff(
        intake_point_id="github:pr_comment",
        actor=Actor.system(),
        input_text=await _render_batch(waiting, session=session),
    )
    run_id = await start_run(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=bindings[0].pipeline_id,
        kickoff=kickoff,
        session=session,
    )
    for row in waiting:
        row.claimed_by_run_id = run_id
    await session.flush()


async def evaluate_auto_approval(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """Enabled + conditions pass + not already approved → `vcs.approve_pr`.
    Skips yaaos-authored PRs (GitHub forbids self-approval)."""
    raise NotImplementedError


async def list_comments_for_run(run_id: UUID, *, session: AsyncSession) -> list[PRComment]:
    """Consumed by the `reply_to_comment` action, where conversation policy executes."""
    rows = (
        (await session.execute(select(PRCommentRow).where(PRCommentRow.claimed_by_run_id == run_id)))
        .scalars()
        .all()
    )
    return [PRComment.from_row(row) for row in rows]


async def _comment_finding_ids_for_run(run_id: UUID, session: AsyncSession) -> tuple[UUID, ...]:
    """Registered with `domain/pipelines.register_comment_findings_provider`
    at import time — see `apps/backend/app/domain/pr_review/__init__.py`.
    Finding ids referenced by this run's claimed comment batch, regardless
    of the finding's own status."""
    rows = (
        (
            await session.execute(
                select(PRCommentRow.finding_id).where(
                    PRCommentRow.claimed_by_run_id == run_id, PRCommentRow.finding_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return tuple(fid for fid in rows if fid is not None)


@task("pr_review.after_run_terminal", queue="pipelines", max_retries=1)
async def _after_run_terminal_task(*, org_id: str, ticket_id: str) -> None:
    async with db_session() as s:
        await maybe_start_batch_run(UUID(org_id), UUID(ticket_id), session=s)
        await s.commit()


AFTER_RUN_TERMINAL: TaskRef = _after_run_terminal_task
