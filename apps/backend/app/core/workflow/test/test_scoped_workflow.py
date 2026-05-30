"""scoped_workflow context manager — isolated-registration contract."""

from __future__ import annotations

import pytest

from app.core.workflow import (
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowNotFoundError,
    get_engine,
)
from app.core.workflow.types import CommandCategory
from app.testing.workflow_harness import scoped_workflow


class _NoopLocal:
    kind = "ScopedTestNoop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


_TEMP_WORKFLOW = Workflow(
    name="scoped-temp-test",
    version=1,
    steps=(
        Step(
            id="only",
            command_kind="ScopedTestNoop",
            transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
        ),
    ),
    entry_step_id="only",
)


@pytest.fixture(autouse=True)
def _register_noop_command() -> None:
    """Register the test command once; idempotent across tests in this file."""
    eng = get_engine()
    if "ScopedTestNoop" not in eng.registered_command_kinds():
        eng.register_command(_NoopLocal())


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
