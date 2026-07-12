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
should start a run, and re-evaluates auto-approval.

`evaluate_auto_approval` approves an externally-authored PR once the repo's
enabled conditions pass against posted-finding state and no approval is
currently active — idempotent by construction, since GitHub is the source
of truth for approval state (a dismiss-on-push cycle simply re-approves at
the next terminal). It skips a yaaos-authored PR (GitHub forbids
self-approval): a ticket's `branch_name` is either intake-supplied (the PR's
own head-branch label, for a PR ticket) or minted by `tickets.mint_branch_name`
(always `yaaos/...`, for a dev/troubleshoot/schedule ticket) — the `yaaos/`
prefix is therefore a reliable, plugin-agnostic signal that yaaos authored
the branch (and, transitively, the PR opened from it).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_keys import get as api_key_get
from app.core.audit_log import Actor, ActorKind, audit_for_pr
from app.core.auth import org_context
from app.core.database import session as db_session
from app.core.intake import parse_yaaos_command
from app.core.tasks import TaskRef, enqueue, task
from app.core.vcs import approve_pr, has_active_approval, post_comment_reply
from app.domain.findings import AutoApproveConditions, find_by_external_comment
from app.domain.findings import evaluate_auto_approve as findings_evaluate_auto_approve
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
from app.domain.repos import get_settings as get_repo_settings
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
    from app.core.identity import find_user_ids_by_github_username  # noqa: PLC0415
    from app.core.tenancy import list_active_member_ids  # noqa: PLC0415

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
    triggered_by_user_id: UUID | None = None
    if author_login:
        user_ids = await find_user_ids_by_github_username(author_login, session=session)
        if len(user_ids) == 1:
            member_ids = set(await list_active_member_ids(session, org_id))
            if user_ids[0] in member_ids:
                triggered_by_user_id = user_ids[0]
    for binding in bindings:
        await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=binding.pipeline_id,
            kickoff=kickoff,
            triggered_by_user_id=triggered_by_user_id,
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
            api_key = await api_key_get(org_id, "anthropic", session=s)
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
    from app.core.identity import find_user_ids_by_github_username  # noqa: PLC0415
    from app.core.tenancy import list_active_member_ids  # noqa: PLC0415

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
                # `id` breaks the tie — comments sharing a `created_at` still
                # render into the batch in a stable, reproducible order.
                .order_by(PRCommentRow.created_at, PRCommentRow.id)
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

    # Resolve PR author → triggered_by_user_id for per-user credential mode.
    triggered_by_user_id: UUID | None = None
    if ticket.pr_id is not None:
        pr = await get_pull_request(ticket.pr_id, org_id=org_id)
        author_login = pr.author_login
        if author_login:
            user_ids = await find_user_ids_by_github_username(author_login, session=session)
            if len(user_ids) == 1:
                member_ids = set(await list_active_member_ids(session, org_id))
                if user_ids[0] in member_ids:
                    triggered_by_user_id = user_ids[0]

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
        triggered_by_user_id=triggered_by_user_id,
        session=session,
    )
    for row in waiting:
        row.claimed_by_run_id = run_id
    await session.flush()


class _AutoApproveSkippedPayload(BaseModel):
    reason: str


async def evaluate_auto_approval(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """Enabled + conditions pass + not already approved → `vcs.approve_pr`.
    Never merges. Skips yaaos-authored PRs (GitHub forbids self-approval —
    the same rule Renovate solves with its `renovate-approve` companion
    app) with an audit-visible reason; a yaaos-approver companion App is a
    separate ticket. Idempotent by construction: GitHub is the source of
    truth for approval state, so a dismiss-on-push cycle simply re-approves
    at the next terminal — no local marker to reconcile."""
    ticket = await get_ticket(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return

    settings = await get_repo_settings(org_id, ticket.repo_external_id, session=session)
    if not settings.auto_approve_enabled:
        return

    pr = await get_pull_request(ticket.pr_id, org_id=org_id)

    if ticket.branch_name is not None and ticket.branch_name.startswith("yaaos/"):
        await audit_for_pr(
            pr.id,
            "pull_request.auto_approve_skipped",
            _AutoApproveSkippedPayload(reason="yaaos_authored_pr"),
            actor=Actor.system(),
            org_id=org_id,
            session=session,
        )
        return

    conditions = AutoApproveConditions.model_validate(settings.auto_approve_conditions)
    passed = await findings_evaluate_auto_approve(org_id, ticket_id, conditions=conditions, session=session)
    if not passed:
        return

    if await has_active_approval(ticket.plugin_id, org_id, pr.external_id):
        return

    await approve_pr(ticket.plugin_id, org_id, pr.external_id)


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
        await evaluate_auto_approval(UUID(org_id), UUID(ticket_id), session=s)
        await s.commit()


AFTER_RUN_TERMINAL: TaskRef = _after_run_terminal_task
