"""set_engine_for_tests — isolated-engine contract.

Asserts that `set_engine_for_tests` provides a fresh engine for the block,
that the recording scenario records start calls without executing workflows,
and that the prior engine is restored on exit (even on exception).
"""

from __future__ import annotations

import pytest

from app.core.workflow import (
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowNotFoundError,
    set_engine_for_tests,
    step,
)


class _NoopLocal:
    kind = "SetEngineTestNoop"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx, *, session=None) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        return Outcome.success()


_noop_step = step(_NoopLocal)
_TEMP_WORKFLOW = Workflow(
    name="set-engine-test-temp",
    version=1,
    steps=(_noop_step,),
    entry=_noop_step,
    transitions={_noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
)


def test_set_engine_for_tests_gives_fresh_engine() -> None:
    """Engine inside the block is fresh (no pre-existing registrations)."""
    with set_engine_for_tests() as eng:
        eng.register_workflow(_TEMP_WORKFLOW)
        assert "set-engine-test-temp" in eng.registered_workflow_names()


def test_set_engine_for_tests_restores_after_exit() -> None:
    """Prior engine is restored once the block exits normally."""
    import app.core.workflow.service as svc  # noqa: PLC0415

    prior = svc._engine  # capture BEFORE the block installs a fresh one
    with set_engine_for_tests() as eng:
        assert eng is not prior
    assert svc._engine is prior  # restored


def test_set_engine_for_tests_restores_on_exception() -> None:
    """Prior engine is restored even when an exception propagates."""
    import app.core.workflow.service as svc  # noqa: PLC0415

    prior = svc._engine

    with pytest.raises(RuntimeError, match="test-error"):
        with set_engine_for_tests():
            raise RuntimeError("test-error")

    assert svc._engine is prior


def test_set_engine_for_tests_recording_scenario() -> None:
    """scenario='recording' yields a _RecordingWorkflowEngine whose
    start_calls list accumulates calls without touching the DB."""
    from uuid import uuid4  # noqa: PLC0415

    with set_engine_for_tests(scenario="recording") as eng:
        # Recording engine start never raises WorkflowNotFoundError.
        ticket_id = str(uuid4())
        import asyncio  # noqa: PLC0415

        result = asyncio.get_event_loop().run_until_complete(
            eng.start(
                workflow_name="anything",
                ticket_id=ticket_id,
                session=None,  # type: ignore[arg-type]
            )
        )
        assert isinstance(result, str)
        assert len(eng.start_calls) == 1
        assert eng.start_calls[0]["workflow_name"] == "anything"
        assert eng.start_calls[0]["ticket_id"] == ticket_id


def test_workflow_not_found_inside_default_engine() -> None:
    """Default engine raises WorkflowNotFoundError for unknown workflow."""
    with set_engine_for_tests() as eng:
        with pytest.raises(WorkflowNotFoundError):
            eng.get_workflow("not-registered")
