"""Workflow / StepRef / Outcome type validation."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.core.workflow import (
    Empty,
    Outcome,
    OutcomeKind,
    RetryPolicy,
    TerminalAction,
    Workflow,
    step,
)


class _NullCmd:
    kind = "Null"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: BaseModel, ctx) -> Outcome:  # type: ignore[no-untyped-def]
        return Outcome.success()


def test_workflow_step_by_step_id() -> None:
    a = step(_NullCmd)

    class _B:
        kind = "B"
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: BaseModel, ctx) -> Outcome:  # type: ignore[no-untyped-def]
            return Outcome.success()

    b = step(_B)
    wf = Workflow(name="x", version=1, steps=(a, b), entry=a)
    assert wf.step_by_step_id("Null") is a
    assert wf.step_by_step_id("missing") is None


def test_retry_policy_validation() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValidationError):
        RetryPolicy(backoff_seconds=-1)


def test_outcome_factories() -> None:
    s = Outcome.success(outputs={"workspace_id": "abc"})
    assert s.kind is OutcomeKind.SUCCESS
    # _DynModel supports both dict equality and item access for backward compat.
    assert s.outputs == {"workspace_id": "abc"}
    assert s.outputs["workspace_id"] == "abc"
    assert s.failure_reason is None
    assert s.hitl_question is None

    f = Outcome.failure(reason="boom")
    assert f.kind is OutcomeKind.FAILURE
    assert f.failure_reason == "boom"

    h = Outcome.hitl_pending(question={"prompt": "approve?"})
    assert h.kind is OutcomeKind.HITL_PENDING
    assert h.hitl_question == {"prompt": "approve?"}


def test_outcome_with_typed_outputs() -> None:
    class _Out(BaseModel):
        workspace_id: str

    out = _Out(workspace_id="ws-123")
    s = Outcome.success(outputs=out)
    assert s.outputs is out
    assert s.outputs.workspace_id == "ws-123"  # type: ignore[attr-defined]


def test_outcome_outputs_default_is_empty() -> None:
    s = Outcome.success()
    assert isinstance(s.outputs, Empty)
    assert s.outputs.model_dump() == {}


def test_terminal_action_in_transitions_is_valid() -> None:
    a = step(_NullCmd)
    wf = Workflow(
        name="x",
        version=1,
        steps=(a,),
        entry=a,
        transitions={a: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    assert wf.transitions[a]["success"] is TerminalAction.COMPLETE_WORKFLOW


def test_command_discrimination_uses_isinstance() -> None:
    """The engine uses isinstance checks on AgentDispatchCommand/HITLCommand, not a category enum."""
    from app.core.workflow import AgentDispatchCommand, HITLCommand  # noqa: PLC0415

    class _Local:
        kind = "TestLocal"
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs, ctx, *, session=None) -> Outcome:  # type: ignore[no-untyped-def]
            return Outcome.success()

    cmd = _Local()
    assert not isinstance(cmd, AgentDispatchCommand)
    assert not isinstance(cmd, HITLCommand)


def test_empty_model_frozen() -> None:
    e = Empty()
    with pytest.raises(ValidationError):
        e.x = "oops"  # type: ignore[attr-defined]


def test_step_ref_equality_based_on_command_class_and_step_id() -> None:
    a = step(_NullCmd)
    b = step(_NullCmd)
    assert a == b  # frozen dataclass — same fields → equal
    assert hash(a) == hash(b)
