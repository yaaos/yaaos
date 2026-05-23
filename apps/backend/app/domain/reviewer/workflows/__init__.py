"""Workflow definitions for the five M05 reviewer task modes.

Each `Workflow` is registered against `core/workflow.get_engine()` at
import time (via `domain/reviewer/__init__.py`). The matching commands
live in `domain/reviewer/commands/` + `core/workspace/commands/` (workspace
lifecycle).

Step `inputs` reference shorthand:
- `$<step_id>.<field>` — value from a prior step's outputs (e.g.
  `$provision.workspace_id`).
- `$ticket.<field>` — value from the ticket payload supplied to
  `engine.start(ticket_payload=...)` by intake.

Required ticket-payload fields by workflow (intake handlers populate):
- `pr_review_v1` + `incremental_review_v1` — `head_sha`, `base_sha`.
- `verify_fix_v1` — `finding_id`, `head_sha`.
- `stale_check_v1` — `finding_ids` (list of candidate finding ids).
- `answer_question_v1` — `finding_id`, `question_body`, `head_sha`.

Future intake handlers MUST populate these — the workflow design assumes
they're there. Missing fields resolve to `None` in command bodies; bodies
that hard-require a field should fail explicitly.
"""

from __future__ import annotations

from app.core.workflow import RetryPolicy, Step, TerminalAction, Workflow

# pr_review_v1: full-PR review.
# CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace
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
            # Pre-flight secrets gate. Posts a warning Review and terminates
            # the workflow if the diff contains a known secret pattern.
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
        ),
        Step(
            id="post",
            command_kind="PostFindings",
            inputs={
                "draft_findings": "$review.draft_findings",
                "workspace_id": "$provision.workspace_id",
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
            command_kind="IncrementalReview",
            inputs={
                "workspace_id": "$provision.workspace_id",
                "head_sha": "$ticket.head_sha",
                "base_sha": "$ticket.base_sha",
            },
        ),
        Step(
            id="post",
            command_kind="PostFindings",
            inputs={
                "draft_findings": "$review.draft_findings",
                "workspace_id": "$provision.workspace_id",
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
            inputs={
                "workspace_id": "$provision.workspace_id",
                "finding_id": "$ticket.finding_id",
                "head_sha": "$ticket.head_sha",
            },
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
            inputs={
                "workspace_id": "$provision.workspace_id",
                "finding_ids": "$ticket.finding_ids",
                "head_sha": "$ticket.head_sha",
            },
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
            inputs={
                "workspace_id": "$provision.workspace_id",
                "finding_id": "$ticket.finding_id",
                "question_body": "$ticket.question_body",
                "head_sha": "$ticket.head_sha",
            },
        ),
        Step(
            id="reply",
            command_kind="PostReply",
            inputs={
                "reply_body": "$answer.reply_body",
                "finding_id": "$ticket.finding_id",
            },
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
