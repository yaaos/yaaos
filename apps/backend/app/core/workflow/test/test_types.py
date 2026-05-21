"""Workflow / Step / Outcome type validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.workflow import (
    CommandCategory,
    Outcome,
    OutcomeKind,
    RetryPolicy,
    Step,
    TerminalAction,
    Workflow,
)


def _step(id_: str, kind: str = "Noop", **kw) -> Step:
    return Step(id=id_, command_kind=kind, **kw)


def test_workflow_step_by_id() -> None:
    wf = Workflow(name="x", version=1, steps=(_step("a"), _step("b")), entry_step_id="a")
    assert wf.step_by_id("a").id == "a"
    assert wf.step_by_id("missing") is None


def test_retry_policy_validation() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValidationError):
        RetryPolicy(backoff_seconds=-1)


def test_outcome_factories() -> None:
    s = Outcome.success(outputs={"workspace_id": "abc"})
    assert s.kind is OutcomeKind.SUCCESS
    assert s.outputs == {"workspace_id": "abc"}
    assert s.failure_reason is None
    assert s.hitl_question is None

    f = Outcome.failure(reason="boom")
    assert f.kind is OutcomeKind.FAILURE
    assert f.failure_reason == "boom"

    h = Outcome.hitl_pending(question={"prompt": "approve?"})
    assert h.kind is OutcomeKind.HITL_PENDING
    assert h.hitl_question == {"prompt": "approve?"}


def test_outcome_append_steps_carries_steps() -> None:
    extra = (_step("plan"), _step("implement"))
    s = Outcome.success(append_steps=extra)
    assert s.append_steps == extra


def test_terminal_action_in_transitions_is_valid() -> None:
    wf = Workflow(
        name="x",
        version=1,
        steps=(_step("a", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}),),
        entry_step_id="a",
    )
    assert wf.steps[0].transitions["success"] is TerminalAction.COMPLETE_WORKFLOW


def test_command_category_enum_values() -> None:
    # Sanity that the three categories are present and string-stable.
    assert {c.value for c in CommandCategory} == {"workspace", "local", "hitl"}
