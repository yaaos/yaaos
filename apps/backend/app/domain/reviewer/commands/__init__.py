"""Reviewer WorkflowCommands.

Workspace commands:
- `CodeReview` — full-PR review dispatched to the remote agent; the terminal
  event's `output` (from the run-sink) is consumed by `PostFindings`.

Local commands:
- `CheckShouldReview` — admission gate before workspace provisioning.
- `SecretsScan` — pre-flight secrets detection.
- `PostFindings` — parse agent `output` → `FindingRow` rows via `publish.py`.

All commands receive typed Pydantic `Inputs` models (populated by the
workflow's `inputs_factory` lambdas) and return typed `Outputs` models.
No context-provider lookups at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict

from app.core.database import session as db_session
from app.core.workflow import CommandCategory, CommandContext, Outcome

if TYPE_CHECKING:
    pass

log = structlog.get_logger("domain.reviewer.commands")

# Labels whose presence on a PR force-skips the review. Case-insensitive.
SKIP_LABELS: frozenset[str] = frozenset({"yaaos-skip", "no-review", "wip"})


def _activity_publisher_for(ctx: CommandContext):  # type: ignore[no-untyped-def]
    """Build an `on_activity` callback that fan-outs each `ActivityEvent` to SSE."""

    async def _publisher(event):  # type: ignore[no-untyped-def]
        from app.core.auth import require_org_context  # noqa: PLC0415
        from app.core.sse import publish_workspace_activity  # noqa: PLC0415

        try:
            await publish_workspace_activity(
                org_id=require_org_context(),
                workflow_execution_id=UUID(ctx.workflow_execution_id),
                payload=event.model_dump(mode="json"),
            )
        except Exception as exc:
            # inside-span failure: workflow.command.* span is active when callback fires
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            log.exception(
                "workspace_review.activity_publish_failed",
                workflow_execution_id=ctx.workflow_execution_id,
            )

    return _publisher


def _record_exc_on_span(exc: Exception) -> None:
    """Record an exception + ERROR status on the active OTel span."""
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(StatusCode.ERROR, str(exc))


# ── Input / Output types ────────────────────────────────────────────────


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


class SecretsScanInputs(BaseModel):
    """Typed inputs for SecretsScan. Populated from the TicketSnapshot."""

    model_config = ConfigDict(frozen=True)
    org_id: UUID
    plugin_id: str
    pr_external_id: str | None = None


class SecretsScanOutputs(BaseModel):
    """Rule ID when a secret is detected; None otherwise."""

    model_config = ConfigDict(frozen=True)
    rule_id: str | None = None


class CodeReviewInputs(BaseModel):
    """Typed inputs for CodeReview. The workspace_id comes from the prior
    ProvisionWorkspace step's outputs; remaining fields from TicketSnapshot."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    org_id: UUID
    repo_external_id: str
    pr_external_id: str
    head_sha: str
    base_sha: str | None = None


class CodeReviewOutputs(BaseModel):
    """Raw skill output string from the InvokeClaudeCode terminal event."""

    model_config = ConfigDict(frozen=True)
    output: str = ""


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


# ── CodeReview ──────────────────────────────────────────────────────────────


def _build_code_review_invocation(inputs: CodeReviewInputs, anthropic_api_key: str) -> object:
    """Pure helper: build the `Invocation` for a PR review from typed inputs.

    `anthropic_api_key` is passed in (not fetched here) because `build_invocation` is
    sync/pure and cannot do async I/O — the caller loads it from BYOK before calling.
    """
    from app.core.coding_agent import Invocation  # noqa: PLC0415
    from app.domain.reviewer.types import ReviewContext, finding_output_schema  # noqa: PLC0415

    review_ctx = ReviewContext(
        org_id=inputs.org_id,
        repo_external_id=inputs.repo_external_id,
        pr_external_id=inputs.pr_external_id,
        head_sha=inputs.head_sha,
        base_sha=inputs.base_sha or "",
        output_schema=finding_output_schema(),
    )
    return Invocation(
        skill="pr_review",
        model="opus",
        effort="medium",
        context={**review_ctx.model_dump(mode="json"), "anthropic_api_key": anthropic_api_key},
        wallclock_seconds=1200,
    )


class CodeReview:
    """Full-PR review. Builds an `InvokeClaudeCode` AgentCommand via
    `coding_agent.dispatch_invocation`, claims the workspace, dispatches,
    and parks. The terminal event's `output` (contributed by the run-sink)
    is parsed by `PostFindings`.
    """

    kind = "CodeReview"
    category = CommandCategory.WORKSPACE
    restart_safe = True
    Inputs = CodeReviewInputs
    Outputs = CodeReviewOutputs

    async def execute(self, inputs: CodeReviewInputs, ctx: CommandContext) -> Outcome:
        # The remote model dispatches via `dispatch`; `execute` is never called
        # by the engine's Workspace branch in normal operation. Retained so the
        # command satisfies the Local execute shape for test harnesses that
        # exercise commands directly.
        del inputs, ctx
        return Outcome.failure(reason="CodeReview.execute is not the dispatch path for remote review")

    async def dispatch(
        self,
        inputs: CodeReviewInputs,
        ctx: CommandContext,
        *,
        session: object,
    ) -> UUID:
        from app.core import byok, coding_agent  # noqa: PLC0415

        anthropic_api_key = await byok.get(inputs.org_id, "anthropic", session=session)  # type: ignore[arg-type]
        if anthropic_api_key is None:
            raise RuntimeError(f"no Anthropic API key for org {inputs.org_id}; add one in Org Settings")
        plugin = coding_agent.get_plugin("claude_code")
        invocation = _build_code_review_invocation(inputs, anthropic_api_key)
        try:
            return await coding_agent.dispatch_invocation(
                workspace_id=inputs.workspace_id,
                invocation=invocation,
                plugin=plugin,
                ctx=ctx,
                session=session,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _record_exc_on_span(exc)
            raise


# ── Local command base ──────────────────────────────────────────────────────


class _LocalReviewCommand:
    category = CommandCategory.LOCAL
    restart_safe = True


# ── CheckShouldReview ────────────────────────────────────────────────────────


class CheckShouldReview:
    """Admission gate before provisioning.

    Returns `Outcome.success(label='skip')` when the PR is draft / fork /
    bot-authored / skip-labelled; the `pr_review_v1` workflow terminates.
    Reads all required fields from typed `CheckShouldReviewInputs` — no DB
    lookups at execute time.
    """

    kind = "CheckShouldReview"
    category = CommandCategory.LOCAL
    restart_safe = True
    Inputs = CheckShouldReviewInputs
    Outputs = CheckShouldReviewOutputs

    async def execute(self, inputs: CheckShouldReviewInputs, ctx: CommandContext) -> Outcome:
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


# ── SecretsScan ──────────────────────────────────────────────────────────────


class SecretsScan:
    """Pre-flight secrets gate.

    Fetches the PR diff and runs `secrets_detection.detect_secrets`. On a
    match returns `Outcome.success(label="skip")` and posts a warning Review.
    Reads all required fields from typed `SecretsScanInputs` — no provider
    lookups at execute time.
    """

    kind = "SecretsScan"
    category = CommandCategory.LOCAL
    restart_safe = True
    Inputs = SecretsScanInputs
    Outputs = SecretsScanOutputs

    async def execute(self, inputs: SecretsScanInputs, ctx: CommandContext) -> Outcome:
        if not inputs.pr_external_id:
            return Outcome.success(outputs=SecretsScanOutputs(rule_id=None))

        from app.core import vcs as _vcs  # noqa: PLC0415
        from app.domain.reviewer.secrets_detection import (  # noqa: PLC0415
            detect_secrets,
            secrets_warning_body,
        )

        try:
            diff = await _vcs.fetch_diff(inputs.plugin_id, inputs.org_id, inputs.pr_external_id)
        except Exception as exc:
            log.warning(
                "secrets_scan.diff_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return Outcome.success(outputs=SecretsScanOutputs(rule_id=None))

        rule_id = detect_secrets(diff)
        if rule_id is None:
            return Outcome.success(outputs=SecretsScanOutputs(rule_id=None))

        try:
            await _vcs.post_comment(
                inputs.plugin_id,
                inputs.org_id,
                inputs.pr_external_id,
                body=secrets_warning_body(rule_id),
            )
        except Exception as exc:
            # inside-span failure: workflow.command.SecretsScan span is active
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            log.exception(
                "secrets_scan.post_warning_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                rule_id=rule_id,
            )

        log.info(
            "secrets_scan.detected",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            rule_id=rule_id,
        )
        return Outcome.success(
            label="skip",
            outputs=SecretsScanOutputs(rule_id=rule_id),
        )


# ── PostFindings ─────────────────────────────────────────────────────────────


class PostFindings(_LocalReviewCommand):
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
    Inputs = PostFindingsInputs
    Outputs = PostFindingsOutputs

    async def execute(self, inputs: PostFindingsInputs, ctx: CommandContext) -> Outcome:
        from app.domain.reviewer.publish import publish_findings  # noqa: PLC0415
        from app.domain.reviewer.service import refresh_ticket_findings_summary  # noqa: PLC0415
        from app.domain.tickets import PullRequestNotFoundError, get_pull_request  # noqa: PLC0415

        # Parse and validate output before touching any external state.
        if not inputs.output:
            # No output from the agent — zero findings, nothing to post.
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
            # inside-span failure: workflow.command.PostFindings span is active
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


__all__ = [
    "CheckShouldReview",
    "CheckShouldReviewInputs",
    "CheckShouldReviewOutputs",
    "CodeReview",
    "CodeReviewInputs",
    "CodeReviewOutputs",
    "PostFindings",
    "PostFindingsInputs",
    "PostFindingsOutputs",
    "SecretsScan",
    "SecretsScanInputs",
    "SecretsScanOutputs",
]
