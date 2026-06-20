"""PostFindings — parse agent output → findings LocalCommand.

Reads the `output` string from `CodeReviewOutputs`, parses it via
`parse_review_output`, and persists + posts findings via `publish_findings`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict

from app.core.database import session as db_session
from app.core.workflow import CommandContext, Outcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("domain.reviewer.commands.post_findings")


class PostFindingsInputs(BaseModel):
    """Typed inputs for PostFindings. `output` comes from CodeReviewOutputs;
    remaining fields from TicketSnapshot."""

    model_config = ConfigDict(frozen=True)
    output: str = ""
    org_id: UUID
    pr_id: UUID | None = None
    pr_external_id: str | None = None
    vcs_plugin_id: str = ""


class PostFindingsOutputs(BaseModel):
    """Count of findings admitted by the reviewer."""

    model_config = ConfigDict(frozen=True)
    admitted_count: int = 0


class PostFindings:
    """Parse the review step's output into `ReportedFinding`s and persist them.

    Reads all required fields from typed `PostFindingsInputs`:
    - `output`: parsed skill output string from the CodeReview terminal event.
    - `org_id`, `pr_id`, `pr_external_id`, `vcs_plugin_id`: from TicketSnapshot.

    Calls `parse_review_output(output)` → `list[ReportedFinding]`,
    then `publish_findings` which validates severity/confidence, assigns
    `finding_display_id`, persists, and posts via VCS.

    Non-conforming output (parse failure OR out-of-range enum values) →
    `Outcome.failure(reason="schema_invalid")` → FAIL_WORKFLOW.
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
        # `session` accepted for LocalCommand Protocol contract; ignored until
        # Phase 7 wires the transactional use. PostFindings opens its own
        # session for the publish + summary writes in the interim.
        del session

        from app.domain.reviewer.publish import publish_findings  # noqa: PLC0415
        from app.domain.reviewer.service import refresh_ticket_findings_summary  # noqa: PLC0415
        from app.domain.tickets import PullRequestNotFoundError, get_pull_request  # noqa: PLC0415

        # Parse and validate output before touching any external state.
        if not inputs.output:
            return Outcome.success(outputs=PostFindingsOutputs(admitted_count=0))

        from app.domain.reviewer.types import parse_review_output  # noqa: PLC0415

        try:
            findings = parse_review_output(inputs.output)
        except ValueError as exc:
            log.warning(
                "post_findings.parse_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
                error=str(exc),
            )
            return Outcome.failure(reason="schema_invalid")

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

        try:
            async with db_session() as s:
                _review, admitted = await publish_findings(
                    pr_id=inputs.pr_id,
                    org_id=inputs.org_id,
                    pr_external_id=inputs.pr_external_id or pr_row.external_id,
                    vcs_plugin_id=inputs.vcs_plugin_id or pr_row.plugin_id,
                    findings=findings,
                    session=s,
                )
                await refresh_ticket_findings_summary(
                    UUID(ctx.ticket_id),
                    inputs.pr_id,
                    org_id=inputs.org_id,
                    session=s,
                )
                await s.commit()
        except ValueError as exc:
            return Outcome.failure(reason=f"finding validation failed: {exc}")
        except Exception as exc:
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            log.exception(
                "post_findings.failed",
                workflow_execution_id=ctx.workflow_execution_id,
                pr_id=str(inputs.pr_id),
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        log.info(
            "post_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            admitted=len(admitted),
        )
        return Outcome.success(outputs=PostFindingsOutputs(admitted_count=len(admitted)))
