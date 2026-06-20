"""CheckShouldReview — admission gate LocalCommand.

Returns `Outcome.success(label='skip')` when the PR is draft, fork,
bot-authored, or skip-labelled. Otherwise returns `Outcome.success()` to
advance to the next workflow step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from app.core.workflow import CommandContext, Outcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("domain.reviewer.commands.check_should_review")

# Labels whose presence on a PR force-skips the review. Case-insensitive.
SKIP_LABELS: frozenset[str] = frozenset({"yaaos-skip", "no-review", "wip"})


class CheckShouldReviewInputs(BaseModel):
    """Typed inputs for CheckShouldReview. Populated from the TicketSnapshot
    workflow input by the workflow's inputs_factory lambda."""

    model_config = ConfigDict(frozen=True)
    is_draft: bool = False
    is_fork: bool = False
    labels: tuple[str, ...] = ()
    author_login: str | None = None


class CheckShouldReviewOutputs(BaseModel):
    """Skip reason when CheckShouldReview gates the workflow."""

    model_config = ConfigDict(frozen=True)
    skip_reason: str | None = None


class CheckShouldReview:
    """Admission gate before provisioning.

    Returns `Outcome.success(label='skip')` when the PR is draft / fork /
    bot-authored / skip-labelled; the `pr_review_v1` workflow terminates.
    Reads all required fields from typed `CheckShouldReviewInputs` — no DB
    lookups at execute time.
    """

    kind = "CheckShouldReview"
    restart_safe = True
    Inputs = CheckShouldReviewInputs
    Outputs = CheckShouldReviewOutputs

    async def execute(
        self,
        inputs: CheckShouldReviewInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> Outcome:
        del session
        reason = _decide_skip(inputs)
        if reason is not None:
            log.debug(
                "checkshouldreview.skip",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
                reason=reason,
            )
            return Outcome.success(label="skip", outputs=CheckShouldReviewOutputs(skip_reason=reason))
        return Outcome.success(outputs=CheckShouldReviewOutputs())


def _decide_skip(inputs: CheckShouldReviewInputs) -> str | None:
    if inputs.is_draft:
        return "draft"
    if inputs.is_fork:
        return "fork"
    labels = {str(label).lower() for label in inputs.labels}
    forced = labels & {label.lower() for label in SKIP_LABELS}
    if forced:
        return f"label:{sorted(forced)[0]}"
    author = (inputs.author_login or "").lower()
    if author.endswith("[bot]") or author.endswith("-bot"):
        return "bot_author"
    return None
