"""Reviewer WorkflowCommands.

AgentDispatchCommand:
- `CodeReview` — full-PR review dispatched to the remote agent via `CodingAgentCommand`.
  Terminal event's `output` (from the run-sink) is consumed by `PostFindings`.

LocalCommands:
- `CheckShouldReview` — admission gate before workspace provisioning.
- `SecretsScan` — pre-flight secrets detection.
- `PostFindings` — parse agent `output` → `FindingRow` rows via `publish.py`.

All commands receive typed Pydantic `Inputs` models (populated by the
workflow's `inputs_factory` lambdas) and return typed `Outputs` models.
"""

from app.domain.reviewer.commands.check_should_review import (
    CheckShouldReview,
    CheckShouldReviewInputs,
    CheckShouldReviewOutputs,
)
from app.domain.reviewer.commands.code_review import (
    CodeReview,
    CodeReviewInputs,
    CodeReviewOutputs,
)
from app.domain.reviewer.commands.post_findings import (
    PostFindings,
    PostFindingsInputs,
    PostFindingsOutputs,
)
from app.domain.reviewer.commands.secrets_scan import (
    SecretsScan,
    SecretsScanInputs,
    SecretsScanOutputs,
)

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
