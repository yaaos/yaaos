"""GitHub-contributed `domain/actions.Action`s: `github:create_pr`,
`github:update_pr`, and `github:reply_to_comment`.

All three are pure control-plane calls — the stage's own exit-push already
put the work branch on the remote; these actions only talk to the GitHub
API and stamp durable state (`tickets.pr_id`, `findings.external_comment_id`,
`findings.defended_at`, finding status).

`_post_residuals` is the posting primitive shared by `create_pr`/`update_pr`.
It is externally idempotent: a mid-body crash may have already posted a
comment on GitHub before the DB write anchoring it landed, so before
posting a finding it reconciles against `vcs.list_yaaos_comments` — every
finding's own `handle` (e.g. `SPEC-001`) rides verbatim in `rule_violated`,
the one `vcs.post_finding` argument this call fully controls, so a literal
substring match against already-posted comment bodies is exact and
independent of whatever cosmetic label the category-derived rendering
produces.

`reply_to_comment` carries the deterministic dispute policy — see its own
docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.vcs import create_pr, fetch_pr, list_yaaos_comments, post_comment_reply, post_finding
from app.core.vcs import resolve_finding_thread as vcs_resolve_finding_thread
from app.domain.actions import ActionContext, ActionError
from app.domain.findings import FindingStatusEvent, dismiss, mark_defended, set_external_anchor
from app.domain.findings import get as get_finding
from app.domain.pr_review import list_comments_for_run
from app.domain.tickets import attach_pr_to_ticket
from app.domain.tickets import get as get_ticket
from app.domain.tickets import upsert as upsert_pull_request
from app.plugins.github.service import get_plugin as get_github_plugin

_ACCEPTANCE_REPLY = "Dismissing this finding based on your feedback."


async def _post_residuals(ctx: ActionContext, pr_external_id: str, *, session: AsyncSession) -> list[UUID]:
    """Post every not-yet-anchored residual finding to the PR; returns the
    ids of the findings this call anchored (fresh post or reconciled)."""
    if not ctx.preceding_residuals:
        return []
    existing = await list_yaaos_comments(ctx.vcs_plugin_id, ctx.org_id, pr_external_id)
    posted: list[UUID] = []
    for finding in ctx.preceding_residuals:
        if finding.external_comment_id is not None:
            continue
        match = next((c for c in existing if finding.handle in c.body), None)
        if match is not None:
            comment_external_id = match.external_id
        else:
            comment_external_id = await post_finding(
                ctx.vcs_plugin_id,
                ctx.org_id,
                pr_external_id,
                file=finding.code_file,
                line_start=finding.code_line,
                line_end=finding.code_line,
                severity=finding.severity,
                category=finding.severity,
                confidence="n/a",
                finding_display_id=finding.display_id,
                rationale=finding.body,
                rule_violated=finding.handle,
                rule_source=finding.source_stage_name,
                suggested_fix=None,
            )
        await set_external_anchor(finding.id, comment_external_id=comment_external_id, session=session)
        posted.append(finding.id)
    return posted


class _CreatePRResult(BaseModel):
    pr_url: str
    pr_external_id: str
    posted_finding_ids: list[UUID]


class _UpdatePRResult(BaseModel):
    posted: list[UUID]
    resolved: list[UUID]
    reflagged: list[UUID]


class GitHubCreatePRAction:
    """Opens the PR for a yaaos-authored ticket's work branch (idempotent —
    `vcs.create_pr` finds the existing PR for `head_branch` on retry), then
    posts the preceding stage's residual findings. A ticket that already has
    a PR (an externally-authored review ticket that reaches this action via
    a definition that opens with `create_pr` anyway) skips straight to
    posting — opening is a no-op."""

    action_id = "github:create_pr"
    plugin_id: str | None = "github"
    label = "Open pull request"
    Result: ClassVar[type[BaseModel]] = _CreatePRResult

    async def execute(self, ctx: ActionContext, *, session: AsyncSession) -> BaseModel:
        ticket = await get_ticket(ctx.ticket_id, org_id=ctx.org_id)
        if ticket.pr_id is None:
            base_branch = await get_github_plugin().get_default_branch(ctx.org_id, ctx.repo_external_id)
            pr_external_id = await create_pr(
                ctx.vcs_plugin_id,
                ctx.org_id,
                ctx.repo_external_id,
                head_branch=ctx.branch_name,
                base_branch=base_branch,
                title=ticket.title,
                body=ctx.kickoff_input or "",
            )
            wire_pr = await fetch_pr(ctx.vcs_plugin_id, ctx.org_id, pr_external_id)
            pr_row = await upsert_pull_request(
                wire_pr, ticket_id=ctx.ticket_id, org_id=ctx.org_id, session=session
            )
            await attach_pr_to_ticket(ctx.ticket_id, org_id=ctx.org_id, pr_id=pr_row.id, session=session)
        else:
            assert ctx.pr_external_id is not None  # _build_action_context resolves it from ticket.pr_id
            pr_external_id = ctx.pr_external_id
            wire_pr = await fetch_pr(ctx.vcs_plugin_id, ctx.org_id, pr_external_id)

        posted = await _post_residuals(ctx, pr_external_id, session=session)
        return _CreatePRResult(
            pr_url=wire_pr.html_url, pr_external_id=pr_external_id, posted_finding_ids=posted
        )


class GitHubUpdatePRAction:
    """Posts new residual findings the same way `create_pr` does, then
    reflects the preceding review stage's mechanically-applied verdicts back
    onto the PR: resolves the thread of every `fixed` finding
    (`vcs.resolve_finding_thread`) and posts any verdict `reply` text into
    the finding's own comment thread. The engine already applied the status
    transition (`domain/findings.resolve`/`reflag`/`reopen`) before this
    action runs — this only makes GitHub reflect it."""

    action_id = "github:update_pr"
    plugin_id: str | None = "github"
    label = "Update pull request"
    Result: ClassVar[type[BaseModel]] = _UpdatePRResult

    async def execute(self, ctx: ActionContext, *, session: AsyncSession) -> BaseModel:
        if ctx.pr_external_id is None:
            raise ActionError("github:update_pr requires a ticket with a bound PR")
        pr_external_id = ctx.pr_external_id

        posted = await _post_residuals(ctx, pr_external_id, session=session)

        resolved: list[UUID] = []
        reflagged: list[UUID] = []
        for verdict in ctx.preceding_verdicts:
            if verdict.status not in ("fixed", "still_present"):
                continue
            finding = await get_finding(verdict.finding_id, session=session)
            if finding.external_comment_id is not None:
                if verdict.status == "fixed":
                    await vcs_resolve_finding_thread(
                        ctx.vcs_plugin_id, ctx.org_id, pr_external_id, finding.external_comment_id
                    )
                if verdict.reply:
                    await post_comment_reply(
                        ctx.vcs_plugin_id,
                        ctx.org_id,
                        pr_external_id,
                        finding.external_comment_id,
                        verdict.reply,
                    )
            if verdict.status == "fixed":
                resolved.append(finding.id)
            else:
                reflagged.append(finding.id)

        return _UpdatePRResult(posted=posted, resolved=resolved, reflagged=reflagged)


class _ReplyToCommentResult(BaseModel):
    replies_posted: list[UUID]
    dismissed: list[UUID]
    defended: list[UUID]


class GitHubReplyToCommentAction:
    """Posts the comment-response review stage's verdict replies into each
    finding's own comment thread, and executes the deterministic dispute
    policy for findings a batched comment disputed:

    - dispute + verdict `None` + a reply → `findings.mark_defended` (the one
      defense — the skill answered without overriding the finding).
    - dispute on an already-defended finding + any non-`user_overrode`
      verdict → coerce: `findings.dismiss` + a generic acceptance reply
      (deterministic — a second dispute always ends in dismiss regardless
      of what the skill now asserts).

    The engine has already applied `fixed`/`still_present`/`user_overrode`
    mechanically before this action runs; a `user_overrode` verdict means
    the finding is already dismissed, so it's excluded from the coercion
    check by name.
    """

    action_id = "github:reply_to_comment"
    plugin_id: str | None = "github"
    label = "Reply to PR comments"
    Result: ClassVar[type[BaseModel]] = _ReplyToCommentResult

    async def execute(self, ctx: ActionContext, *, session: AsyncSession) -> BaseModel:
        if ctx.pr_external_id is None:
            raise ActionError("github:reply_to_comment requires a ticket with a bound PR")
        pr_external_id = ctx.pr_external_id

        comments = await list_comments_for_run(ctx.run_id, session=session)
        disputed_finding_ids = {
            c.finding_id for c in comments if c.classification == "dispute" and c.finding_id is not None
        }

        replies_posted: list[UUID] = []
        dismissed: list[UUID] = []
        defended: list[UUID] = []

        for verdict in ctx.preceding_verdicts:
            finding = await get_finding(verdict.finding_id, session=session)
            reply_text = verdict.reply

            if verdict.finding_id in disputed_finding_ids:
                if verdict.status is None:
                    if verdict.reply:
                        await mark_defended(verdict.finding_id, session=session)
                        defended.append(verdict.finding_id)
                elif finding.defended_at is not None and verdict.status != "user_overrode":
                    event = FindingStatusEvent(
                        status="dismissed",
                        method="user_overrode",
                        actor=Actor.system(),
                        run_id=ctx.run_id,
                        at=datetime.now(UTC),
                    )
                    await dismiss(verdict.finding_id, event=event, session=session)
                    dismissed.append(verdict.finding_id)
                    reply_text = reply_text or _ACCEPTANCE_REPLY

            if finding.external_comment_id is not None and reply_text:
                await post_comment_reply(
                    ctx.vcs_plugin_id, ctx.org_id, pr_external_id, finding.external_comment_id, reply_text
                )
                replies_posted.append(verdict.finding_id)

        return _ReplyToCommentResult(replies_posted=replies_posted, dismissed=dismissed, defended=defended)
