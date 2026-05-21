"""Reviewer WorkflowCommands for the five M05 task modes.

Five **Workspace** commands wrap `domain/coding_agent` invocations against
a workspace:
- `CodeReview` — full-PR review.
- `IncrementalReview` — push-driven incremental review against a base sha.
- `VerifyFix` — ack a developer's "is this fixed?" reply on a finding.
- `StaleCheck` — periodic check that an open finding still applies.
- `AnswerQuestion` — answer a developer @yaaos-mention on a finding.

Five **Local** commands handle the control-plane side:
- `CheckShouldReview` — admission gating (draft/skip-label/external-contrib/
  org-config) before any workspace is provisioned.
- `PostFindings` — admit findings via the aggregate, post to GitHub.
- `ResolveFinding` — close a finding's thread on a verified fix.
- `ArchiveStaleFindings` — mark stale findings archived.
- `PostReply` — post a reply on a finding's thread.

Phase 4 (foundations) ships stub bodies (returning `Outcome.success()`) so
the engine's registry is complete and the five reviewer workflows can
register cleanly. Real bodies — wired to `domain/coding_agent` +
`domain/reviewer.admission` (extracted from `queue.py`) — land in the
follow-on Phase 4 iteration that dismantles `queue.py` and drops
`review_jobs`.
"""

from __future__ import annotations

from typing import Any

from app.core.workflow import CommandCategory, CommandContext, Outcome

# ── Workspace commands (5) ──────────────────────────────────────────────


class _WorkspaceReviewCommand:
    """Workspace-category reviewer command. Each wraps a `domain/coding_agent`
    invocation in the full implementation."""

    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


class CodeReview(_WorkspaceReviewCommand):
    kind = "CodeReview"


class IncrementalReview(_WorkspaceReviewCommand):
    kind = "IncrementalReview"


class VerifyFix(_WorkspaceReviewCommand):
    kind = "VerifyFix"


class StaleCheck(_WorkspaceReviewCommand):
    kind = "StaleCheck"


class AnswerQuestion(_WorkspaceReviewCommand):
    kind = "AnswerQuestion"


# ── Local commands (5) ──────────────────────────────────────────────────


class _LocalReviewCommand:
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


class CheckShouldReview(_LocalReviewCommand):
    """Admission gate before provisioning. Returns `Outcome.success(label='skip')`
    when the PR is draft / fork / bot-authored / skip-labelled / off org config;
    workflow then terminates without spinning up a workspace."""

    kind = "CheckShouldReview"


class PostFindings(_LocalReviewCommand):
    kind = "PostFindings"


class ResolveFinding(_LocalReviewCommand):
    kind = "ResolveFinding"


class ArchiveStaleFindings(_LocalReviewCommand):
    kind = "ArchiveStaleFindings"


class PostReply(_LocalReviewCommand):
    kind = "PostReply"


ALL_WORKSPACE_COMMANDS: tuple[_WorkspaceReviewCommand, ...] = (
    CodeReview(),
    IncrementalReview(),
    VerifyFix(),
    StaleCheck(),
    AnswerQuestion(),
)

ALL_LOCAL_COMMANDS: tuple[_LocalReviewCommand, ...] = (
    CheckShouldReview(),
    PostFindings(),
    ResolveFinding(),
    ArchiveStaleFindings(),
    PostReply(),
)


__all__ = [
    "ALL_LOCAL_COMMANDS",
    "ALL_WORKSPACE_COMMANDS",
    "AnswerQuestion",
    "ArchiveStaleFindings",
    "CheckShouldReview",
    "CodeReview",
    "IncrementalReview",
    "PostFindings",
    "PostReply",
    "ResolveFinding",
    "StaleCheck",
    "VerifyFix",
]
