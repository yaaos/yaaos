"""Workflow definitions for the reviewer.

Only `pr_review_v1` is registered. Incremental review, verify-fix,
stale-check, and answer-question workflows are not wired.
"""

from __future__ import annotations

from app.core.workflow import RetryPolicy, Step, TerminalAction, Workflow

# pr_review_v1: CheckShouldReview → SecretsScan → ProvisionWorkspace →
#               CodeReview → PostFindings → CleanupWorkspace
#
# On any terminal failure, the engine runs the `cleanup` finalizer step once
# before recording `failed`, ensuring the workspace is reaped even on hard
# failures. On the success path `cleanup` runs as the normal terminal step.
pr_review_v1 = Workflow(
    name="pr_review_v1",
    version=1,
    steps=(
        Step(
            id="check",
            command_kind="CheckShouldReview",
            transitions={
                "skip": TerminalAction.COMPLETE_WORKFLOW,
                "failure": TerminalAction.FAIL_WORKFLOW,
            },
        ),
        Step(
            id="secrets",
            command_kind="SecretsScan",
            transitions={
                "skip": TerminalAction.COMPLETE_WORKFLOW,
                "failure": TerminalAction.FAIL_WORKFLOW,
            },
        ),
        Step(
            id="provision",
            command_kind="ProvisionWorkspace",
            retry_policy=RetryPolicy(max_attempts=2),
        ),
        Step(
            id="review",
            command_kind="CodeReview",
            inputs={
                "workspace_id": "$provision.workspace_id",
                "head_sha": "$ticket.head_sha",
                "base_sha": "$ticket.base_sha",
            },
            transitions={
                "failure": TerminalAction.FAIL_WORKFLOW,
            },
        ),
        Step(
            id="post",
            command_kind="PostFindings",
            inputs={
                "stdout": "$review.stdout",
                "workspace_id": "$provision.workspace_id",
            },
            transitions={
                "failure": TerminalAction.FAIL_WORKFLOW,
            },
        ),
        Step(
            id="cleanup",
            command_kind="CleanupWorkspace",
            inputs={"workspace_id": "$provision.workspace_id"},
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="check",
    finalizer_step_id="cleanup",
)


ALL_WORKFLOWS: tuple[Workflow, ...] = (pr_review_v1,)


__all__ = [
    "ALL_WORKFLOWS",
    "pr_review_v1",
]
