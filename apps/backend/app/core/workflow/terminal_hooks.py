"""Terminal-hook registry for `core/workflow`.

Hooks registered here are awaited by the engine's `_enter_terminal_state`
helper at every workflow terminal transition (done / failed / cancelled).
They run inside the engine's terminal-commit transaction — a raising hook
rolls back the entire terminal write.

Hook contract (keyword-only):

    async def my_hook(
        *,
        workflow_execution_id: UUID,
        workflow_name: str,
        ticket_id: UUID,
        org_id: UUID,
        terminal_state: WorkflowState,
        failure_reason: str | None,
        session: AsyncSession,
    ) -> None:
        ...

Hooks receive only primitives — no SQLAlchemy rows cross the boundary.
The `session` is the engine's current transaction; hooks may read/write
to it but must not commit (the engine commits after all hooks return).

The registry is empty in production by default; callers register hooks at
startup time via explicit calls (same pattern as the recovery-policy
registry in `recovery.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# Type alias for a terminal hook callable (keyword-only args, async).
TerminalHook = Callable[..., Awaitable[None]]

_TERMINAL_HOOKS: list[TerminalHook] = []


def register_terminal_hook(hook: TerminalHook) -> None:
    """Register a hook to be awaited on every terminal workflow transition.

    Idempotent — registering the same callable object twice is a no-op
    (identity check), so an accidental double call at startup is harmless.
    Hooks are invoked in registration order.
    """
    if hook not in _TERMINAL_HOOKS:
        _TERMINAL_HOOKS.append(hook)


def get_terminal_hooks() -> list[TerminalHook]:
    """Return a snapshot of the registered hooks (copy, not the live list)."""
    return list(_TERMINAL_HOOKS)


def _clear_terminal_hooks_for_tests() -> None:
    """Clear all registered terminal hooks. For test isolation only —
    never call from production code."""
    _TERMINAL_HOOKS.clear()
