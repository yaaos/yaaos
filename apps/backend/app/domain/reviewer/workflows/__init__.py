"""Workflow definitions for the reviewer.

Only `pr_review_v1` is registered. Incremental review, verify-fix,
stale-check, and answer-question workflows are not wired.
"""

from __future__ import annotations

from app.core.workflow import (
    RetryPolicy,
    TerminalAction,
    Workflow,
    get_step_output,
    step,
    workflow_input,
)
from app.core.workspace import (
    CleanupWorkspace,
    CleanupWorkspaceInputs,
    ProvisionWorkspace,
    ProvisionWorkspaceInputs,
    RefreshWorkspaceAuth,
)
from app.domain.reviewer.commands import (
    CheckShouldReview,
    CheckShouldReviewInputs,
    CodeReview,
    CodeReviewInputs,
    PostFindings,
    PostFindingsInputs,
    SecretsScan,
    SecretsScanInputs,
)
from app.domain.reviewer.types import TicketSnapshot
from app.domain.tickets import transition_ticket_on_start, transition_ticket_on_terminal

# ── pr_review_v1 ────────────────────────────────────────────────────────────
#
# Typed workflow using StepRef + WorkflowInputRef. All fields come from
# the TicketSnapshot passed to engine.start(workflow_input=...) and from
# prior step outputs accessed via the ContextVar mechanism.
#
# Step sequence:
#   CheckShouldReview → SecretsScan → ProvisionWorkspace →
#   CodeReview → PostFindings → CleanupWorkspace
#
# Finalizer: CleanupWorkspace fires on any terminal failure before recording
# `failed`, ensuring the workspace is reaped even on hard failures. On the
# success path `cleanup` runs as the normal terminal step.

ticket = workflow_input(TicketSnapshot)

check = step(
    CheckShouldReview,
    inputs=lambda: CheckShouldReviewInputs(
        is_draft=ticket.outputs.is_draft,
        is_fork=ticket.outputs.is_fork,
        labels=ticket.outputs.labels,
        author_login=ticket.outputs.author_login,
    ),
)

secrets = step(
    SecretsScan,
    inputs=lambda: SecretsScanInputs(
        org_id=ticket.outputs.org_id,
        plugin_id=ticket.outputs.plugin_id,
        pr_external_id=ticket.outputs.pr_external_id,
    ),
)

provision = step(
    ProvisionWorkspace,
    inputs=lambda: ProvisionWorkspaceInputs(
        org_id=ticket.outputs.org_id,
        plugin_id=ticket.outputs.plugin_id,
        repo_external_id=ticket.outputs.repo_external_id,
        head_sha=ticket.outputs.head_sha,
        base_sha=ticket.outputs.base_sha,
    ),
    retry_policy=RetryPolicy(max_attempts=2),
)

review = step(
    CodeReview,
    inputs=lambda: CodeReviewInputs(
        workspace_id=provision.outputs.workspace_id,
        org_id=ticket.outputs.org_id,
        repo_external_id=ticket.outputs.repo_external_id,
        pr_external_id=ticket.outputs.pr_external_id or "",
        head_sha=ticket.outputs.head_sha,
        base_sha=ticket.outputs.base_sha,
    ),
)


def _cleanup_ws_id() -> CleanupWorkspaceInputs:
    """Safe accessor for CleanupWorkspace inputs — handles the case where
    ProvisionWorkspace didn't complete (provision.outputs doesn't exist in
    the ContextVar yet, so workspace_id is None)."""
    prov_out = get_step_output(provision.step_id)
    ws_id = prov_out.workspace_id if prov_out is not None and hasattr(prov_out, "workspace_id") else None  # type: ignore[union-attr]
    return CleanupWorkspaceInputs(workspace_id=ws_id)


post = step(
    PostFindings,
    inputs=lambda: PostFindingsInputs(
        output=review.outputs.output,
        org_id=ticket.outputs.org_id,
        pr_id=ticket.outputs.pr_id,
        pr_external_id=ticket.outputs.pr_external_id,
        vcs_plugin_id=ticket.outputs.plugin_id,
    ),
)

cleanup = step(CleanupWorkspace, inputs=_cleanup_ws_id)

pr_review_v1 = Workflow(
    name="pr_review_v1",
    version=1,
    steps=(check, secrets, provision, review, post, cleanup),
    entry=check,
    transitions={
        check: {
            "skip": TerminalAction.COMPLETE_WORKFLOW,
            "failure": TerminalAction.FAIL_WORKFLOW,
        },
        secrets: {
            "skip": TerminalAction.COMPLETE_WORKFLOW,
            "failure": TerminalAction.FAIL_WORKFLOW,
        },
        review: {
            "failure": TerminalAction.FAIL_WORKFLOW,
        },
        post: {
            "failure": TerminalAction.FAIL_WORKFLOW,
        },
        cleanup: {
            "success": TerminalAction.COMPLETE_WORKFLOW,
        },
    },
    finalizer=cleanup,
    workflow_input=ticket,
    recovery_commands=(RefreshWorkspaceAuth,),
    on_start=transition_ticket_on_start,
    on_terminal=transition_ticket_on_terminal,
)


ALL_WORKFLOWS: tuple[Workflow, ...] = (pr_review_v1,)


__all__ = [
    "ALL_WORKFLOWS",
    "pr_review_v1",
]
