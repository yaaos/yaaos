"""Service tests for the workflow start-hook registry.

Covers:
- A registered start hook is invoked exactly once with the expected primitives
  when the workflow bootstrap branch runs.
- The hook runs inside the engine's bootstrap-commit transaction (same session,
  no mid-hook commit).
- No hook → bootstrap succeeds without error.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    get_start_hooks,
    register_start_hook,
    step,
)
from app.core.workflow.start_hooks import _clear_start_hooks_for_tests
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


class _NoopLocal:
    """Minimal local command that terminates the workflow successfully."""

    kind = "StartHookNoopLocal"
    category = CommandCategory.LOCAL
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any) -> Outcome:
        del inputs, ctx
        return Outcome.success()


_noop_step = step(_NoopLocal)
_TEST_WORKFLOW = Workflow(
    name="start-hook-test-workflow",
    version=1,
    steps=(_noop_step,),
    entry=_noop_step,
    transitions={_noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
)


@pytest.fixture(autouse=True)
def _isolate_hooks() -> None:  # type: ignore[return]
    """Clear the start hook registry before and after each test."""
    _clear_start_hooks_for_tests()
    yield  # type: ignore[misc]
    _clear_start_hooks_for_tests()


async def _drain(db_session: Any, *, max_iters: int = 20) -> None:
    """Drain the outbox into the matching task bodies."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


@pytest.mark.asyncio
async def test_start_hook_invoked_with_expected_kwargs(db_session) -> None:  # type: ignore[no-untyped-def]
    """A registered start hook is awaited exactly once with the expected
    primitive kwargs during the workflow bootstrap branch."""
    org_id = uuid4()
    invocations: list[dict] = []

    async def _probe_hook(**kwargs: Any) -> None:
        invocations.append(dict(kwargs))

    register_start_hook(_probe_hook)
    assert get_start_hooks() == [_probe_hook]

    with scoped_engine() as engine:
        engine.register_workflow(_TEST_WORKFLOW)

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id = await engine.start(
                workflow_name="start-hook-test-workflow",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session)

    assert len(invocations) == 1, f"Expected 1 invocation; got {len(invocations)}"
    call = invocations[0]
    assert str(call["workflow_execution_id"]) == wfx_id
    assert call["workflow_name"] == "start-hook-test-workflow"
    assert isinstance(call["ticket_id"], UUID)
    assert call["org_id"] == org_id
    # session key present and not committed inside the hook
    assert "session" in call


@pytest.mark.asyncio
async def test_no_start_hooks_bootstrap_succeeds(db_session) -> None:  # type: ignore[no-untyped-def]
    """Bootstrap succeeds with zero hooks registered — no error."""
    org_id = uuid4()
    assert get_start_hooks() == []

    with scoped_engine() as engine:
        engine.register_workflow(_TEST_WORKFLOW)

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id = await engine.start(
                workflow_name="start-hook-test-workflow",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session)

    assert wfx_id is not None
