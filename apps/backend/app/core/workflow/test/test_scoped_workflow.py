"""scoped_workflow context manager — isolated-registration contract."""

from __future__ import annotations

import pytest

from app.core.workflow import (
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowNotFoundError,
    get_engine,
    step,
)
from app.testing.workflow_harness import scoped_workflow


class _NoopLocal:
    kind = "ScopedTestNoop"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx, *, session=None) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        return Outcome.success()


_noop_step = step(_NoopLocal)
_TEMP_WORKFLOW = Workflow(
    name="scoped-temp-test",
    version=1,
    steps=(_noop_step,),
    entry=_noop_step,
    transitions={_noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
)


def test_scoped_workflow_registers_while_inside() -> None:
    """Workflow is findable inside the block."""
    eng = get_engine()

    with scoped_workflow(_TEMP_WORKFLOW):
        wf = eng.get_workflow("scoped-temp-test")
        assert wf.name == "scoped-temp-test"


def test_scoped_workflow_unregisters_after_exit() -> None:
    """Workflow is gone once the block exits normally."""
    eng = get_engine()

    with scoped_workflow(_TEMP_WORKFLOW):
        pass

    with pytest.raises(WorkflowNotFoundError):
        eng.get_workflow("scoped-temp-test")


def test_scoped_workflow_unregisters_on_exception() -> None:
    """Workflow is unregistered even when an exception propagates."""
    eng = get_engine()

    with pytest.raises(RuntimeError, match="test-error"):
        with scoped_workflow(_TEMP_WORKFLOW):
            raise RuntimeError("test-error")

    with pytest.raises(WorkflowNotFoundError):
        eng.get_workflow("scoped-temp-test")


def test_scoped_workflow_yields_spec() -> None:
    """The context manager yields the same Workflow object it received."""
    with scoped_workflow(_TEMP_WORKFLOW) as wf:
        assert wf is _TEMP_WORKFLOW
