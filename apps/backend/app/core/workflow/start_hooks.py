"""Start-hook registry for `core/workflow`.

Hooks registered here are awaited by the engine's `_route_workflow_impl`
bootstrap branch (when `completed_step_id is None` and the workflow first
transitions to RUNNING). They run inside the engine's bootstrap-commit
transaction — a raising hook rolls back the entire bootstrap write.

Hook contract (keyword-only):

    async def my_hook(
        *,
        workflow_execution_id: UUID,
        workflow_name: str,
        ticket_id: UUID,
        org_id: UUID,
        session: AsyncSession,
    ) -> None: ...

Hooks receive only primitives — no SQLAlchemy rows cross the boundary.
The `session` is the engine's current transaction; hooks may read/write
to it but must NOT commit (the engine commits after all hooks return).

The registry is empty in production by default; callers register hooks at
startup via explicit calls (same pattern as terminal_hooks.py).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

StartHook = Callable[..., Awaitable[None]]

_START_HOOKS: list[StartHook] = []


def register_start_hook(hook: StartHook) -> None:
    """Register a hook to be awaited on every workflow bootstrap transition.

    Idempotent — registering the same callable object twice is a no-op
    (identity check), so an accidental double-registration at startup is
    harmless. Hooks are invoked in registration order.
    """
    if hook not in _START_HOOKS:
        _START_HOOKS.append(hook)


def get_start_hooks() -> list[StartHook]:
    """Return a snapshot of the registered hooks (copy, not the live list)."""
    return list(_START_HOOKS)


def _clear_start_hooks_for_tests() -> None:
    """Clear all registered start hooks. For test isolation only —
    never call from production code."""
    _START_HOOKS.clear()
