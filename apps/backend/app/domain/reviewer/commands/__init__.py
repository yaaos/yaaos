"""Reviewer WorkflowCommands.

Workspace commands:
- `CodeReview` — full-PR review dispatched to the remote agent; the terminal
  event's `stdout` is consumed by `PostFindings`.

Local commands:
- `CheckShouldReview` — admission gate before workspace provisioning.
- `SecretsScan` — pre-flight secrets detection.
- `PostFindings` — parse agent `stdout` → `FindingRow` rows via `publish.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.database import session as db_session
from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace import (
    dispatch_invoke_claude_code,
    get_workflow_context_provider,
    get_workspace_owner,
    try_claim,
)
from app.domain.tickets import get_payload as get_ticket_payload

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
        except Exception:
            log.exception(
                "workspace_review.activity_publish_failed",
                workflow_execution_id=ctx.workflow_execution_id,
            )

    return _publisher


# ── CodeReview ──────────────────────────────────────────────────────────────


class CodeReview:
    """Full-PR review. Builds an `InvokeClaudeCode` AgentCommand via
    `coding_agent.build_review_invocation`, claims the workspace, dispatches,
    and parks. The terminal event's `stdout` is parsed by `PostFindings`.
    """

    kind = "CodeReview"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        # The remote model dispatches via `dispatch`; `execute` is never called
        # by the engine's Workspace branch in normal operation. Retained so the
        # command satisfies the Local execute shape for test harnesses that
        # exercise commands directly.
        del inputs, ctx
        return Outcome.failure(reason="CodeReview.execute is not the dispatch path for remote review")

    async def dispatch(
        self,
        inputs: dict[str, Any],
        ctx: CommandContext,
        *,
        session: Any,
    ) -> UUID:
        from uuid import UUID as _UUID  # noqa: PLC0415

        from app.core import coding_agent  # noqa: PLC0415
        from app.core.coding_agent import (  # noqa: PLC0415
            ReviewContext,
            create_run,
            finding_output_schema,
        )

        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            raise RuntimeError("CodeReview.dispatch missing workspace_id input")
        ws_id = _UUID(str(ws_id_raw))

        owner = await get_workspace_owner(ws_id, session=session)
        if owner is None:
            raise RuntimeError(f"workspace {ws_id} not found for CodeReview.dispatch")
        if owner.owning_agent_id is None:
            raise RuntimeError(f"workspace {ws_id} has no owning_agent_id; cannot dispatch review")

        provider = get_workflow_context_provider()
        ticket_ctx = await provider.get_workspace_ticket_context(_UUID(ctx.ticket_id))
        if ticket_ctx is None:
            raise RuntimeError(f"ticket {ctx.ticket_id} not found for CodeReview.dispatch")

        head_sha = str(ticket_ctx.payload.get("head_sha") or inputs.get("head_sha") or "")
        base_sha = str(ticket_ctx.payload.get("base_sha") or inputs.get("base_sha") or "")
        pr_external_id = str(ticket_ctx.payload.get("pr_external_id") or "")

        review_ctx = ReviewContext(
            org_id=ticket_ctx.org_id,
            repo_external_id=ticket_ctx.repo_external_id,
            pr_external_id=pr_external_id,
            head_sha=head_sha,
            base_sha=base_sha,
            output_schema=finding_output_schema(),
        )

        plugin = coding_agent.get_plugin("claude_code")
        try:
            invocation = await plugin.build_review_invocation(review_ctx, session=session)
        except Exception as exc:
            log.exception(
                "code_review.build_invocation_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            raise RuntimeError(f"build_review_invocation failed: {exc}") from exc

        # Enqueue the InvokeClaudeCode command pinned to the owning agent first,
        # then atomically claim the workspace with the returned command_id.
        # This ordering guarantees the AgentCommand row exists before claim.
        command_id = await dispatch_invoke_claude_code(
            ws_id,
            org_id=owner.org_id,
            agent_id=owner.owning_agent_id,
            invocation=invocation.model_dump(mode="json"),
            traceparent=ctx.traceparent or "",
            session=session,
            workflow_execution_id=_UUID(ctx.workflow_execution_id),
        )

        # Create the coding-agent run row in the same transaction so the row
        # is durable iff the dispatch commits. Only InvokeClaudeCode commands
        # get a run row; the `kind` from the invocation is the command_kind.
        run_id = await create_run(
            org_id=owner.org_id,
            workflow_execution_id=_UUID(ctx.workflow_execution_id),
            step_id=ctx.step_id,
            agent_command_id=command_id,
            command_kind=invocation.kind,
            plugin_id=plugin.plugin_id,
            session=session,
        )
        log.info(
            "code_review.run_created",
            run_id=str(run_id),
            command_id=str(command_id),
            workflow_execution_id=ctx.workflow_execution_id,
        )

        claimed = await try_claim(
            ws_id,
            command_id=command_id,
            workflow_execution_id=_UUID(ctx.workflow_execution_id),
            session=session,
        )
        if not claimed:
            raise RuntimeError(f"workspace {ws_id} is busy or inactive; cannot claim for CodeReview")

        log.info(
            "code_review.dispatched",
            workspace_id=str(ws_id),
            command_id=str(command_id),
            workflow_execution_id=ctx.workflow_execution_id,
        )
        return command_id


# ── Local command base ──────────────────────────────────────────────────────


class _LocalReviewCommand:
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


# ── CheckShouldReview ────────────────────────────────────────────────────────


class CheckShouldReview:
    """Admission gate before provisioning.

    Returns `Outcome.success(label='skip')` when the PR is draft / fork /
    bot-authored / skip-labelled; the `pr_review_v1` workflow terminates.
    """

    kind = "CheckShouldReview"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        async with db_session() as s:
            payload = await get_ticket_payload(UUID(ctx.ticket_id), session=s)

        reason = _decide_skip(payload)
        if reason is not None:
            log.info(
                "checkshouldreview.skip",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
                reason=reason,
            )
            return Outcome.success(label="skip", outputs={"reason": reason})

        return Outcome.success(outputs={"pr_external_id": payload.get("pr_external_id")})


def _decide_skip(payload: dict[str, Any]) -> str | None:
    if payload.get("is_draft"):
        return "draft"
    if payload.get("is_fork"):
        return "fork"
    labels = {str(label).lower() for label in (payload.get("labels") or [])}
    forced = labels & {label.lower() for label in SKIP_LABELS}
    if forced:
        return f"label:{sorted(forced)[0]}"
    author = (payload.get("author_login") or "").lower()
    if author.endswith("[bot]") or author.endswith("-bot"):
        return "bot_author"
    return None


# ── SecretsScan ──────────────────────────────────────────────────────────────


class SecretsScan:
    """Pre-flight secrets gate.

    Fetches the PR diff and runs `secrets_detection.detect_secrets`. On a
    match returns `Outcome.success(label="skip")` and posts a warning Review.
    """

    kind = "SecretsScan"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs

        provider = get_workflow_context_provider()
        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "secrets_scan.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            return Outcome.success(outputs={"rule_id": None})

        from app.core.vcs import get_plugin as get_vcs_plugin  # noqa: PLC0415
        from app.domain.reviewer.secrets_detection import (  # noqa: PLC0415
            detect_secrets,
            secrets_warning_body,
        )

        try:
            vcs_plugin = get_vcs_plugin(ticket_ctx.plugin_id)
            pr_external_id = str(ticket_ctx.payload.get("pr_external_id") or "")
            if not pr_external_id:
                return Outcome.success(outputs={"rule_id": None})
            diff = await vcs_plugin.fetch_diff(pr_external_id)
        except Exception as exc:
            log.warning(
                "secrets_scan.diff_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return Outcome.success(outputs={"rule_id": None})

        rule_id = detect_secrets(diff)
        if rule_id is None:
            return Outcome.success(outputs={"rule_id": None})

        try:
            await vcs_plugin.post_comment(pr_external_id, body=secrets_warning_body(rule_id))
        except Exception:
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
            outputs={"reason": "secrets_detected", "rule_id": rule_id},
        )


# ── PostFindings ─────────────────────────────────────────────────────────────


class PostFindings(_LocalReviewCommand):
    """Parse the review step's stdout into `ReportedFinding`s and persist them.

    Inputs:
    - `stdout`: raw stream-json stdout from the `InvokeClaudeCode` terminal event.
    - `workspace_id`: the workspace the review ran against (unused here; kept for
      step-graph symmetry).

    Calls `coding_agent.parse_review_output(stdout)` → `list[ReportedFinding]`,
    then `publish_findings` which validates severity/confidence, assigns
    `finding_display_id`, persists, and posts via VCS.

    Non-conforming stdout (parse failure OR out-of-range enum values) →
    `Outcome.failure(reason="schema_invalid")` → FAIL_WORKFLOW.
    """

    kind = "PostFindings"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        stdout_raw = inputs.get("stdout") or ""

        from app.core import coding_agent  # noqa: PLC0415
        from app.domain.reviewer.publish import publish_findings  # noqa: PLC0415
        from app.domain.reviewer.service import refresh_ticket_findings_summary  # noqa: PLC0415
        from app.domain.tickets import PullRequestNotFoundError, get_pull_request  # noqa: PLC0415

        # Parse and validate stdout before touching any external state.
        if not stdout_raw:
            # No output from the agent — zero findings, nothing to post.
            return Outcome.success(outputs={"admitted_count": 0})
        else:
            plugin = coding_agent.get_plugin("claude_code")
            try:
                findings = plugin.parse_review_output(stdout_raw)
            except ValueError as exc:
                log.warning(
                    "post_findings.parse_failed",
                    workflow_execution_id=ctx.workflow_execution_id,
                    ticket_id=ctx.ticket_id,
                    error=str(exc),
                )
                return Outcome.failure(reason="schema_invalid")

        provider = get_workflow_context_provider()
        ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "post_findings.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"admitted_count": 0})

        try:
            pr_row = await get_pull_request(ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
        except PullRequestNotFoundError:
            pr_row = None

        if pr_row is None:
            log.warning(
                "post_findings.no_pr_row",
                workflow_execution_id=ctx.workflow_execution_id,
                pr_id=str(ticket_ctx.pr_id),
            )
            return Outcome.success(outputs={"admitted_count": 0})

        try:
            from app.core.coding_agent import get_run_id_for_workflow_step  # noqa: PLC0415

            async with db_session() as s:
                # Look up the run created by the preceding CodeReview step so
                # the review row can be linked via reviews.run_id.
                run_id = await get_run_id_for_workflow_step(
                    UUID(ctx.workflow_execution_id),
                    "review",
                    session=s,
                )
                _review, admitted = await publish_findings(
                    pr_id=ticket_ctx.pr_id,
                    org_id=ticket_ctx.org_id,
                    pr_external_id=pr_row.external_id,
                    vcs_plugin_id=pr_row.plugin_id,
                    findings=findings,
                    run_id=run_id,
                    session=s,
                )
                await refresh_ticket_findings_summary(
                    UUID(ctx.ticket_id),
                    ticket_ctx.pr_id,
                    org_id=ticket_ctx.org_id,
                    session=s,
                )
                await s.commit()
        except ValueError as exc:
            return Outcome.failure(reason=f"finding validation failed: {exc}")
        except Exception as exc:
            log.exception(
                "post_findings.failed",
                workflow_execution_id=ctx.workflow_execution_id,
                pr_id=str(ticket_ctx.pr_id),
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        log.info(
            "post_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            admitted=len(admitted),
        )
        return Outcome.success(outputs={"admitted_count": len(admitted)})


ALL_WORKSPACE_COMMANDS: tuple[object, ...] = (CodeReview(),)

ALL_LOCAL_COMMANDS: tuple[object, ...] = (
    CheckShouldReview(),
    SecretsScan(),
    PostFindings(),
)


__all__ = [
    "ALL_LOCAL_COMMANDS",
    "ALL_WORKSPACE_COMMANDS",
    "CheckShouldReview",
    "CodeReview",
    "PostFindings",
    "SecretsScan",
]
