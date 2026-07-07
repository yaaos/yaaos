"""Stub service surface for `domain/pr_review`.

Bodies raise `NotImplementedError` — only the signatures are load-bearing.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.pr_review.types import InboundComment, PRComment


async def handle_pr_comment(
    *, org_id: UUID, ticket_id: UUID, comment: InboundComment, session: AsyncSession
) -> None:
    """Entry from the VCS plugin (bot comments filtered, PR→ticket resolved
    before the call). `@yaaos` grammar first; free text enqueues classification."""
    raise NotImplementedError


async def maybe_start_batch_run(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """No run in flight AND waiting comments exist → claim + batch + start_run."""
    raise NotImplementedError


async def evaluate_auto_approval(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> None:
    """Enabled + conditions pass + not already approved → `vcs.approve_pr`.
    Skips yaaos-authored PRs (GitHub forbids self-approval)."""
    raise NotImplementedError


async def list_comments_for_run(run_id: UUID, *, session: AsyncSession) -> list[PRComment]:
    """Consumed by the `reply_to_comment` action, where conversation policy executes."""
    raise NotImplementedError
