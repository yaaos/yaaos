"""Code-shipped default `PipelineDefinition`s — the six out-of-the-box
pipelines (dev, troubleshoot, implementation, PR review, incremental
review, comment response). Frozen module-level constants, built once at
import time (not reconstructed per call), with pinned uuid7 ids so
`PipelineCallStage` cross-references (dev/troubleshoot -> implementation)
and `service._TEMPLATES_BY_ID`'s by-id lookup stay stable for the life of
the process.

These are never persisted — a new org starts empty. `instantiate_template`
deep-copies one into a normal org `pipelines` row (fresh ids, call targets
rewired); the run engine only ever sees org rows, never these constants
directly (`start_run`/`flatten` read from `pipelines`, not from here).

Skill names referenced below are pinned repo files under
`.claude/skills/<name>/SKILL.md`, all carrying the `pipeline-` prefix
(run-engine consumption, not human invocation): `pipeline-requirements`,
`pipeline-requirements-review`, `pipeline-architecture`,
`pipeline-architecture-review`, `pipeline-plan`, `pipeline-implement`,
`pipeline-code-review`, `pipeline-diagnose`, `pipeline-comment-response`.
Stage names are independent of skill names — `troubleshoot`'s `fix-plan`
stage runs the `pipeline-plan` skill under a stage identity distinct from
`dev`'s own `plan` stage, so the two pipelines never collide on a flattened
stage name if ever composed together.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.pipelines.definition import (
    ActionStage,
    BoundaryControl,
    PipelineCallStage,
    PipelineDefinition,
    ReviewConfig,
    ReviewSkillStage,
    SkillStage,
)

DEV_ID = UUID("019f3e31-8f0e-742e-9d3d-fe943a548478")
TROUBLESHOOT_ID = UUID("019f3e31-8f0e-742e-9d3d-fe95190b8f74")
IMPLEMENTATION_ID = UUID("019f3e31-8f0e-742e-9d3d-fe96e9d1ffe0")
PR_REVIEW_ID = UUID("019f3e31-8f0e-742e-9d3d-fe9700b9c29a")
INCREMENTAL_REVIEW_ID = UUID("019f3e31-8f0e-742e-9d3d-fe98e26dfa92")
COMMENT_RESPONSE_ID = UUID("019f3e31-8f0e-742e-9d3d-fe99ab52e976")

# Light-touch gating for the early planning/investigation stages — a human
# only needs to look when the skill itself is unsure.
_PLANNING_BOUNDARY = BoundaryControl(mode="conditional", on_confidence_below="medium")

# The stage that actually changes code: gate on blocker residuals, low
# confidence, AND protected-path touches (a plan artifact can also trip this
# — `paths_affected` covers planned, not just touched, paths).
_CODE_BOUNDARY = BoundaryControl(
    mode="conditional", on_blocker_residuals=True, on_confidence_below="medium", on_protected_code=True
)

# Standalone review-kind stages (PR review, incremental review, comment
# response) — `on_protected_code` is a structural no-op for these (a
# `SkillReviewReturn` carries no `paths_affected`), so it's left off.
_REVIEW_BOUNDARY = BoundaryControl(
    mode="conditional", on_blocker_residuals=True, on_confidence_below="medium"
)

IMPLEMENTATION = PipelineDefinition(
    id=IMPLEMENTATION_ID,
    name="implementation",
    description=(
        "Implements against the nearest upstream artifact (or the kickoff input directly), "
        "reviewed by code-review, and opens a pull request carrying any residual findings."
    ),
    stages=(
        SkillStage(
            name="implement",
            skill_name="pipeline-implement",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="high",
            review=ReviewConfig(skill_name="pipeline-code-review", max_iterations=3),
            wallclock_seconds=14400,
            boundary=_CODE_BOUNDARY,
        ),
        ActionStage(description="Open pull request", action_id="github:create_pr"),
    ),
)

DEV = PipelineDefinition(
    id=DEV_ID,
    name="dev",
    description=(
        "Hands a feature spec through requirements, architecture, and plan artifacts, "
        "then implements the plan via the implementation pipeline."
    ),
    stages=(
        SkillStage(
            name="requirements",
            skill_name="pipeline-requirements",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="medium",
            review=ReviewConfig(skill_name="pipeline-requirements-review", max_iterations=2),
            boundary=_PLANNING_BOUNDARY,
        ),
        SkillStage(
            name="architecture",
            skill_name="pipeline-architecture",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="medium",
            review=ReviewConfig(skill_name="pipeline-architecture-review", max_iterations=2),
            boundary=_PLANNING_BOUNDARY,
        ),
        SkillStage(
            name="plan",
            skill_name="pipeline-plan",
            coding_agent_plugin_id="claude_code",
            model="claude-opus-4-8",
            effort="xhigh",
            boundary=_PLANNING_BOUNDARY,
        ),
        PipelineCallStage(description="Implement the plan", pipeline_id=IMPLEMENTATION_ID),
    ),
)

TROUBLESHOOT = PipelineDefinition(
    id=TROUBLESHOOT_ID,
    name="troubleshoot",
    description=(
        "Diagnoses a bug report, plans the fix (stage name fix-plan, running the plan skill), "
        "then implements it via the implementation pipeline."
    ),
    stages=(
        SkillStage(
            name="diagnose",
            skill_name="pipeline-diagnose",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="medium",
            boundary=_PLANNING_BOUNDARY,
        ),
        SkillStage(
            name="fix-plan",
            skill_name="pipeline-plan",
            coding_agent_plugin_id="claude_code",
            model="claude-opus-4-8",
            effort="xhigh",
            boundary=_PLANNING_BOUNDARY,
        ),
        PipelineCallStage(description="Implement the fix", pipeline_id=IMPLEMENTATION_ID),
    ),
)

PR_REVIEW = PipelineDefinition(
    id=PR_REVIEW_ID,
    name="PR review",
    description="Reviews a newly-opened PR and posts findings to it.",
    stages=(
        ReviewSkillStage(
            name="code-review",
            skill_name="pipeline-code-review",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="high",
            boundary=_REVIEW_BOUNDARY,
        ),
        ActionStage(description="Update pull request", action_id="github:update_pr"),
    ),
)

INCREMENTAL_REVIEW = PipelineDefinition(
    id=INCREMENTAL_REVIEW_ID,
    name="incremental review",
    description=(
        "Reviews the diff since the PR's last review (code-review skill, same as PR review) "
        "and verdicts the ticket's open findings against it."
    ),
    stages=(
        ReviewSkillStage(
            name="code-review",
            skill_name="pipeline-code-review",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="high",
            boundary=_REVIEW_BOUNDARY,
        ),
        ActionStage(description="Update pull request", action_id="github:update_pr"),
    ),
)

COMMENT_RESPONSE = PipelineDefinition(
    id=COMMENT_RESPONSE_ID,
    name="comment response",
    description="Answers a batch of PR comments — questions, disputes, and fix claims — and replies.",
    stages=(
        ReviewSkillStage(
            name="comment-response",
            skill_name="pipeline-comment-response",
            coding_agent_plugin_id="claude_code",
            model="claude-sonnet-5",
            effort="medium",
            boundary=_REVIEW_BOUNDARY,
        ),
        ActionStage(description="Reply to PR comments", action_id="github:reply_to_comment"),
    ),
)

ALL_DEFAULTS: tuple[PipelineDefinition, ...] = (
    DEV,
    TROUBLESHOOT,
    IMPLEMENTATION,
    PR_REVIEW,
    INCREMENTAL_REVIEW,
    COMMENT_RESPONSE,
)
