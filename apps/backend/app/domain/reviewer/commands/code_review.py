"""CodeReview — full-PR review CodingAgentCommand.

Inherits from `CodingAgentCommand` (which provides `@final dispatch` that calls
`core/coding_agent.dispatch_invocation`). `build_invocation` constructs the
`Invocation` intent; the `@final dispatch` resolves the plugin, compiles the
exec block, parks the workflow in AWAITING_AGENT, and auto-injects
`CodeReview.ExpectedResponse.model_json_schema()` into the invocation context
so the skill prompt carries the validated response schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

from app.core.coding_agent import CodingAgentCommand, Invocation
from app.core.workflow import CommandContext, Outcome
from app.domain.reviewer.types import CodeReviewResponse, ReviewContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("domain.reviewer.commands.code_review")


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
    """Typed outputs from the CodeReview step.

    `response` holds the fully-validated `CodeReviewResponse` parsed by
    `CodingAgentCommand.handle_response` on `completed_success`. The workflow
    lambda for `PostFindings` reads `review.outputs.response.findings`.
    """

    model_config = ConfigDict(frozen=True)
    response: CodeReviewResponse = CodeReviewResponse(findings=[])


class CodeReview(CodingAgentCommand):
    """Full-PR review dispatched to the remote coding agent.

    `build_invocation` constructs the `Invocation` for the `pr_review` skill.
    BYOK API key delivery rides `ConfigUpdate.byok_secrets` at identity exchange.
    The `@final dispatch` (from `CodingAgentCommand`) resolves the `claude_code`
    plugin, calls `plugin.compile_invocation`, delegates to `dispatch_invocation`
    (Layer 3), and auto-injects `CodeReviewResponse.model_json_schema()` into the
    invocation context under `output_schema`.

    On `completed_success` the engine calls `handle_response` (from
    `CodingAgentCommand`) which validates the agent's JSON output against
    `CodeReviewResponse` and emits a typed `Outcome` — no bespoke parsing logic.
    """

    kind = "CodeReview"
    plugin_id = "claude_code"
    Inputs = CodeReviewInputs
    Outputs = CodeReviewOutputs
    ExpectedResponse: ClassVar[type[BaseModel]] = CodeReviewResponse

    async def execute(self, inputs: CodeReviewInputs, ctx: CommandContext) -> Outcome:
        # The engine's AgentDispatch branch never calls `execute` in production.
        # Retained so the command satisfies structural Protocol checks in tests.
        del inputs, ctx
        return Outcome.failure(reason="CodeReview.execute is not the dispatch path for remote review")

    async def build_invocation(
        self,
        inputs: CodeReviewInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> Invocation:
        """Build the Invocation intent for the pr_review skill.

        BYOK delivery for the Anthropic API key rides ConfigUpdate.byok_secrets
        at identity exchange — no per-command key insertion needed here.
        """
        review_ctx = ReviewContext(
            org_id=inputs.org_id,
            repo_external_id=inputs.repo_external_id,
            pr_external_id=inputs.pr_external_id,
            head_sha=inputs.head_sha,
            base_sha=inputs.base_sha or "",
        )
        return Invocation(
            workspace_id=inputs.workspace_id,
            skill="pr_review",
            model="opus",
            effort="medium",
            context=review_ctx.model_dump(mode="json"),
            wallclock_seconds=1200,
        )
