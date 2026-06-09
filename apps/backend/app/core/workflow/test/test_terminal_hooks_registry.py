"""Terminal-hook registry tests — registry lives in core/workflow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

import app.core.workflow.service as _svc
from app.core.workflow import get_terminal_hooks, register_terminal_hook
from app.core.workflow.service import _enter_terminal_state
from app.core.workflow.terminal_hooks import _clear_terminal_hooks_for_tests
from app.core.workflow.types import WorkflowState


@pytest.fixture(autouse=True)
def _isolate() -> None:  # type: ignore[return]
    """Clear the hook registry before and after each test."""
    _clear_terminal_hooks_for_tests()
    yield
    _clear_terminal_hooks_for_tests()


# ── Register / get round-trip ────────────────────────────────────────────


def test_register_and_get() -> None:
    async def my_hook(**kwargs: Any) -> None:
        pass

    register_terminal_hook(my_hook)
    hooks = get_terminal_hooks()
    assert hooks == [my_hook]


def test_get_returns_empty_when_no_hooks_registered() -> None:
    assert get_terminal_hooks() == []


def test_get_returns_copy_not_live_list() -> None:
    """Mutating the returned list must not affect the registry."""

    async def hook_a(**kwargs: Any) -> None:
        pass

    register_terminal_hook(hook_a)
    snapshot = get_terminal_hooks()
    snapshot.clear()
    assert get_terminal_hooks() == [hook_a]


# ── Idempotency (identity deduplication) ────────────────────────────────


def test_double_register_is_idempotent() -> None:
    async def hook_a(**kwargs: Any) -> None:
        pass

    register_terminal_hook(hook_a)
    register_terminal_hook(hook_a)
    assert get_terminal_hooks() == [hook_a]


def test_two_different_hooks_both_registered() -> None:
    async def hook_a(**kwargs: Any) -> None:
        pass

    async def hook_b(**kwargs: Any) -> None:
        pass

    register_terminal_hook(hook_a)
    register_terminal_hook(hook_b)
    hooks = get_terminal_hooks()
    assert hook_a in hooks
    assert hook_b in hooks
    assert len(hooks) == 2


def test_registration_order_preserved() -> None:
    async def first(**kwargs: Any) -> None:
        pass

    async def second(**kwargs: Any) -> None:
        pass

    async def third(**kwargs: Any) -> None:
        pass

    register_terminal_hook(first)
    register_terminal_hook(second)
    register_terminal_hook(third)
    assert get_terminal_hooks() == [first, second, third]


# ── Clear ────────────────────────────────────────────────────────────────


def test_clear_for_tests_empties_registry() -> None:
    async def hook_a(**kwargs: Any) -> None:
        pass

    register_terminal_hook(hook_a)
    _clear_terminal_hooks_for_tests()
    assert get_terminal_hooks() == []


def test_clear_then_register_works() -> None:
    async def hook_a(**kwargs: Any) -> None:
        pass

    register_terminal_hook(hook_a)
    _clear_terminal_hooks_for_tests()

    async def hook_b(**kwargs: Any) -> None:
        pass

    register_terminal_hook(hook_b)
    assert get_terminal_hooks() == [hook_b]


# ── _enter_terminal_state awaits hooks with expected primitive kwargs ────


@pytest.mark.asyncio
async def test_enter_terminal_state_invokes_hook_with_primitives() -> None:
    """_enter_terminal_state must await registered hooks with the expected
    primitive kwargs derived from the workflow execution row."""
    # Build a minimal stand-in for WorkflowExecutionRow.
    wfx = MagicMock()
    exec_id = UUID("00000000-0000-0000-0000-000000000001")
    ticket_id = UUID("00000000-0000-0000-0000-000000000002")
    wfx.id = exec_id
    wfx.workflow_name = "pr_review_v1"
    wfx.ticket_id = ticket_id
    wfx.failure_reason = "provision_failed"
    wfx.state = WorkflowState.RUNNING.value  # will be overwritten

    # A mock session (no real DB needed for this unit test).
    session = MagicMock()

    hook = AsyncMock()
    register_terminal_hook(hook)

    # Patch _workflow_org_id and _publish_state_changed so they're no-ops.
    org_id = UUID("00000000-0000-0000-0000-000000000099")
    original_org_id = _svc._workflow_org_id
    original_publish = _svc._publish_state_changed
    _svc._workflow_org_id = lambda _wfx: org_id  # type: ignore[assignment]
    _svc._publish_state_changed = lambda _s, _wfx: None  # type: ignore[assignment]

    try:
        await _enter_terminal_state(session, wfx, WorkflowState.FAILED)
    finally:
        _svc._workflow_org_id = original_org_id  # type: ignore[assignment]
        _svc._publish_state_changed = original_publish  # type: ignore[assignment]

    hook.assert_awaited_once_with(
        workflow_execution_id=exec_id,
        workflow_name="pr_review_v1",
        ticket_id=ticket_id,
        org_id=org_id,
        terminal_state=WorkflowState.FAILED,
        failure_reason="provision_failed",
        session=session,
    )
    # State must have been written to wfx.
    assert wfx.state == WorkflowState.FAILED.value
