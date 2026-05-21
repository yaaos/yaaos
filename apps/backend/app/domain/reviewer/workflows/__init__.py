"""Workflow definitions for the five M05 reviewer task modes.

Each `Workflow` is registered against `core/workflow.get_engine()` at
import time (via `domain/reviewer/__init__.py`). The matching commands
live in `domain/reviewer/commands/` + `core/workspace/commands/` (workspace
lifecycle). Step input expressions use the `$<step_id>.<field>` form the
router resolves via `step_state`.
"""

from __future__ import annotations

from app.core.workflow import RetryPolicy, Step, TerminalAction, Workflow

# pr_review_v1: full-PR review.
# CheckShouldReview → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace
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
            id="provision",
            command_kind="ProvisionWorkspace",
            retry_policy=RetryPolicy(max_attempts=2),
        ),
        Step(
            id="review",
            command_kind="CodeReview",
            inputs={"workspace_id": "$provision.workspace_id"},
        ),
        Step(
            id="post",
            command_kind="PostFindings",
            inputs={"draft_findings": "$review.draft_findings"},
        ),
        Step(
            id="cleanup",
            command_kind="CleanupWorkspace",
            inputs={"workspace_id": "$provision.workspace_id"},
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="check",
)


# incremental_review_v1: push-driven incremental review.
incremental_review_v1 = Workflow(
    name="incremental_review_v1",
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
            id="provision",
            command_kind="ProvisionWorkspace",
            retry_policy=RetryPolicy(max_attempts=2),
        ),
        Step(
            id="review",
            command_kind="IncrementalReview",
            inputs={"workspace_id": "$provision.workspace_id"},
        ),
        Step(
            id="post",
            command_kind="PostFindings",
            inputs={"draft_findings": "$review.draft_findings"},
        ),
        Step(
            id="cleanup",
            command_kind="CleanupWorkspace",
            inputs={"workspace_id": "$provision.workspace_id"},
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="check",
)


# verify_fix_v1: ack a developer's "is this fixed?" reply on a finding.
verify_fix_v1 = Workflow(
    name="verify_fix_v1",
    version=1,
    steps=(
        Step(
            id="provision",
            command_kind="ProvisionWorkspace",
            retry_policy=RetryPolicy(max_attempts=2),
        ),
        Step(
            id="verify",
            command_kind="VerifyFix",
            inputs={"workspace_id": "$provision.workspace_id"},
        ),
        Step(
            id="resolve",
            command_kind="ResolveFinding",
            inputs={"verdict": "$verify.verdict"},
        ),
        Step(
            id="cleanup",
            command_kind="CleanupWorkspace",
            inputs={"workspace_id": "$provision.workspace_id"},
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="provision",
)


# stale_check_v1: periodic check that an open finding still applies after
# unrelated changes; archives findings that no longer make sense.
stale_check_v1 = Workflow(
    name="stale_check_v1",
    version=1,
    steps=(
        Step(
            id="provision",
            command_kind="ProvisionWorkspace",
            retry_policy=RetryPolicy(max_attempts=2),
        ),
        Step(
            id="check",
            command_kind="StaleCheck",
            inputs={"workspace_id": "$provision.workspace_id"},
        ),
        Step(
            id="archive",
            command_kind="ArchiveStaleFindings",
            inputs={"stale_finding_ids": "$check.stale_finding_ids"},
        ),
        Step(
            id="cleanup",
            command_kind="CleanupWorkspace",
            inputs={"workspace_id": "$provision.workspace_id"},
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="provision",
)


# answer_question_v1: developer @yaaos-mention on a finding asking a question.
answer_question_v1 = Workflow(
    name="answer_question_v1",
    version=1,
    steps=(
        Step(
            id="provision",
            command_kind="ProvisionWorkspace",
            retry_policy=RetryPolicy(max_attempts=2),
        ),
        Step(
            id="answer",
            command_kind="AnswerQuestion",
            inputs={"workspace_id": "$provision.workspace_id"},
        ),
        Step(
            id="reply",
            command_kind="PostReply",
            inputs={"reply_body": "$answer.reply_body"},
        ),
        Step(
            id="cleanup",
            command_kind="CleanupWorkspace",
            inputs={"workspace_id": "$provision.workspace_id"},
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="provision",
)


ALL_WORKFLOWS: tuple[Workflow, ...] = (
    pr_review_v1,
    incremental_review_v1,
    verify_fix_v1,
    stale_check_v1,
    answer_question_v1,
)


__all__ = [
    "ALL_WORKFLOWS",
    "answer_question_v1",
    "incremental_review_v1",
    "pr_review_v1",
    "stale_check_v1",
    "verify_fix_v1",
]
