"""Factory functions for building workflow definitions.

`step()` constructs a `StepRef` from a command class (not an instance).
`workflow_input()` constructs a `WorkflowInputRef` for the typed startup snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.core.workflow.types import RetryPolicy, StepRef, WorkflowInputRef

if TYPE_CHECKING:
    pass


def step(
    command_class: type,
    *,
    inputs: Callable[[], BaseModel] | None = None,
    retry_policy: RetryPolicy | None = None,
) -> StepRef:
    """Construct a `StepRef` from a command class.

    `command_class.kind` becomes `StepRef.step_id` — two calls with the same
    class always produce equal `StepRef` objects when `inputs` is also the same
    (frozen dataclass equality).

    `inputs`: a zero-arg lambda that calls each upstream step's `.outputs`
    property (resolved via the ContextVar populated by `_enqueue_start_step`)
    to build the typed `Inputs` model for this step.

    `retry_policy`: overrides the default `RetryPolicy(max_attempts=1)`.
    """
    return StepRef(
        command_class=command_class,
        step_id=command_class.kind,
        inputs_factory=inputs,
        retry_policy=retry_policy if retry_policy is not None else RetryPolicy(),
    )


def workflow_input(snapshot_type: type[BaseModel]) -> WorkflowInputRef:
    """Construct a `WorkflowInputRef` for the typed workflow-input snapshot.

    The ref's `step_id` is ``"__workflow_input__"``.  Use the returned object
    inside step `inputs` lambdas to access the snapshot fields:

        ticket = workflow_input(TicketSnapshot)
        check = step(CheckShouldReview, inputs=lambda: CheckShouldReviewInputs(
            is_draft=ticket.outputs.is_draft,
            ...
        ))
    """
    return WorkflowInputRef(snapshot_type=snapshot_type)


__all__ = ["step", "workflow_input"]
