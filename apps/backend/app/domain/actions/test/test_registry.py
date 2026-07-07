"""Registry tests for `domain/actions`."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.actions import (
    ActionContext,
    ActionInfo,
    ActionNotFoundError,
    get_action,
    list_actions,
    register_action,
    set_actions_for_tests,
)


class _StubResult(BaseModel):
    ok: bool = True


class _StubAction:
    action_id: str
    plugin_id: str | None = None
    label: str = "Stub action"
    Result: ClassVar[type[BaseModel]] = _StubResult

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id

    async def execute(self, ctx: ActionContext, *, session: AsyncSession) -> BaseModel:
        return _StubResult()


@pytest.fixture(autouse=True)
def _empty_registry():
    """Every test in this module starts from a known-empty registry —
    independent of whatever the outer `actions_registry_isolation` autouse
    fixture copied in (empty at this phase, but explicit beats implicit)."""
    with set_actions_for_tests(scenario="empty"):
        yield


def test_register_and_get_action() -> None:
    action = _StubAction("stub")
    register_action(action)
    assert get_action("stub") is action


def test_register_duplicate_raises() -> None:
    register_action(_StubAction("stub"))
    with pytest.raises(ValueError, match="already registered"):
        register_action(_StubAction("stub"))


def test_get_unknown_action_raises() -> None:
    with pytest.raises(ActionNotFoundError):
        get_action("nope")


def test_list_actions_returns_action_info() -> None:
    register_action(_StubAction("a"))
    register_action(_StubAction("b"))
    infos = list_actions()
    assert {i.action_id for i in infos} == {"a", "b"}
    assert all(isinstance(i, ActionInfo) for i in infos)


def test_set_actions_for_tests_default_scenario_copies_current_registry() -> None:
    register_action(_StubAction("outer"))
    with set_actions_for_tests():
        assert get_action("outer") is not None
        register_action(_StubAction("inner"))
        assert get_action("inner") is not None
    # back outside: "inner" never leaked into the enclosing registry.
    with pytest.raises(ActionNotFoundError):
        get_action("inner")


def test_set_actions_for_tests_empty_scenario_isolates_from_current_registry() -> None:
    register_action(_StubAction("outer"))
    with set_actions_for_tests(scenario="empty"):
        with pytest.raises(ActionNotFoundError):
            get_action("outer")


def test_set_actions_for_tests_restores_prior_binding_on_exit() -> None:
    register_action(_StubAction("outer"))
    with set_actions_for_tests(scenario="empty"):
        register_action(_StubAction("temporary"))
    assert get_action("outer") is not None
    with pytest.raises(ActionNotFoundError):
        get_action("temporary")
