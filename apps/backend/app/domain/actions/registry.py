"""Registry for `domain/actions` — mirrors `CodingAgentRegistry`
(`apps/backend/app/core/coding_agent/service.py:27`).

ContextVar-bound so each test context gets a fresh, isolated instance;
production rides the import-time default for the process lifetime — it
never calls a `bind_*` function. The carve-out is `set_actions_for_tests`,
which yields the bound instance for the duration of a `with` block.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from app.domain.actions.types import Action, ActionInfo, ActionNotFoundError


class ActionRegistry:
    """Action map, keyed by `action_id`."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.action_id in self._actions:
            raise ValueError(f"action {action.action_id!r} already registered")
        self._actions[action.action_id] = action

    def get(self, action_id: str) -> Action:
        try:
            return self._actions[action_id]
        except KeyError as e:
            raise ActionNotFoundError(action_id) from e

    def list(self) -> list[ActionInfo]:
        return [
            ActionInfo(action_id=a.action_id, plugin_id=a.plugin_id, label=a.label)
            for a in self._actions.values()
        ]

    def copy(self) -> ActionRegistry:
        clone = ActionRegistry()
        clone._actions = dict(self._actions)
        return clone


_registry_var: ContextVar[ActionRegistry | None] = ContextVar("_action_registry_var", default=None)


def _get() -> ActionRegistry:
    val = _registry_var.get()
    if val is None:
        val = ActionRegistry()
        _registry_var.set(val)
    return val


def register_action(action: Action) -> None:
    """Register an action. Raises `ValueError` if `action_id` is already taken."""
    _get().register(action)


def get_action(action_id: str) -> Action:
    """Return the registered action. Raises `ActionNotFoundError`."""
    return _get().get(action_id)


def list_actions() -> list[ActionInfo]:
    """Return `{action_id, plugin_id, label}` for every registered action."""
    return _get().list()


@contextmanager
def set_actions_for_tests(*, scenario: Literal["default", "empty"] = "default") -> Iterator[ActionRegistry]:
    """Bind an isolated registry for the duration of the `with` block.

    ``scenario="default"`` gives a copy of the current registry;
    ``scenario="empty"`` gives a brand-new empty registry. Restores the
    prior binding on exit — even on exception.
    """
    instance = ActionRegistry() if scenario == "empty" else _get().copy()
    token = _registry_var.set(instance)
    try:
        yield instance
    finally:
        _registry_var.reset(token)
