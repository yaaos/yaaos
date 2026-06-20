"""PostFindings — typed findings → persist LocalCommand.

Receives a pre-validated `findings: list[ReportedFindingShape]` from the
`CodeReview` step's `handle_response` output. Persists and posts via
`publish_findings`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

from app.core.workflow import CommandContext, Outcome
from app.domain.reviewer.types import ReportedFindingShape

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("domain.reviewer.commands.post_findings")


class PostFindingsInputs(BaseModel):
    """Typed inputs for PostFindings.

    `findings` is the list of validated `ReportedFindingShape` objects produced
    by `CodeReview.handle_response` and passed via the workflow lambda
    `review.outputs.response.findings`. Remaining fields come from TicketSnapshot.
    """

    model_config = ConfigDict(frozen=True)
    findings: list[ReportedFindingShape] = []
    org_id: UUID
    pr_id: UUID | None = None
    pr_external_id: str | None = None
    vcs_plugin_id: str = ""


class PostFindingsOutputs(BaseModel):
    """Count of findings admitted by the reviewer."""

    model_config = ConfigDict(frozen=True)
    admitted_count: int = 0


class PostFindings:
    """Persist typed findings and post via VCS.

    Reads fully-validated `PostFindingsInputs.findings` (already a
    `list[ReportedFindingShape]` — no parsing step). Calls `publish_findings`
    which assigns `finding_display_id`, persists, and posts via VCS.

    The engine passes the outer session so finding rows + the outbox enqueue
    for route_workflow commit atomically. PostFindings must never call
    session.commit().
    """

    kind = "PostFindings"
    restart_safe = True
    Inputs = PostFindingsInputs
    Outputs = PostFindingsOutputs

    async def execute(
        self,
        inputs: PostFindingsInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> Outcome:
        from app.domain.reviewer.publish import publish_findings  # noqa: PLC0415
        from app.domain.reviewer.service import refresh_ticket_findings_summary  # noqa: PLC0415
        from app.domain.tickets import PullRequestNotFoundError, get_pull_request  # noqa: PLC0415

        if inputs.pr_id is None:
            log.debug(
                "post_findings.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs=PostFindingsOutputs(admitted_count=0))

        try:
            pr_row = await get_pull_request(inputs.pr_id, org_id=inputs.org_id)
        except PullRequestNotFoundError:
            pr_row = None

        if pr_row is None:
            log.warning(
                "post_findings.no_pr_row",
                workflow_execution_id=ctx.workflow_execution_id,
                pr_id=str(inputs.pr_id),
            )
            return Outcome.success(outputs=PostFindingsOutputs(admitted_count=0))

        # ValueError from publish_findings (invalid severity/confidence) is a
        # graceful validation failure — return Outcome.failure without polluting
        # the engine's SAVEPOINT rollback path. All other exceptions propagate
        # so the SAVEPOINT rolls back atomically.
        try:
            _review, admitted = await publish_findings(
                pr_id=inputs.pr_id,
                org_id=inputs.org_id,
                pr_external_id=inputs.pr_external_id or pr_row.external_id,
                vcs_plugin_id=inputs.vcs_plugin_id or pr_row.plugin_id,
                findings=inputs.findings,
                session=session,
            )
        except ValueError as exc:
            return Outcome.failure(reason=f"finding validation failed: {exc}")

        await refresh_ticket_findings_summary(
            UUID(ctx.ticket_id),
            inputs.pr_id,
            org_id=inputs.org_id,
            session=session,
        )

        log.info(
            "post_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            admitted=len(admitted),
        )
        return Outcome.success(outputs=PostFindingsOutputs(admitted_count=len(admitted)))
