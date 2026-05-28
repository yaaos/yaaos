"""scoped_task_registration — temporary task registration for tests."""

from __future__ import annotations

import pytest

from app.core.tasks import scoped_task_registration, task
from app.core.tasks.broker import get_broker


@pytest.mark.service
def test_scoped_registration_visible_inside_exits_outside() -> None:
    """Task registered inside the block is found by the broker; gone after exit."""

    async def _temp() -> None:
        return None

    ref = task("scoped_temp")(_temp)
    with scoped_task_registration(ref):
        assert get_broker().find_task("scoped_temp") is not None

    assert get_broker().find_task("scoped_temp") is None


@pytest.mark.service
def test_scoped_registration_cleans_up_on_exception() -> None:
    """Registry entry is removed even when the body raises."""

    async def _err() -> None:
        return None

    ref = task("scoped_err")(_err)
    with pytest.raises(RuntimeError):
        with scoped_task_registration(ref):
            raise RuntimeError("boom")

    assert get_broker().find_task("scoped_err") is None


@pytest.mark.service
def test_scoped_registration_names_are_independent() -> None:
    """Two different scoped registrations use distinct names and clean up independently."""

    async def _x() -> None:
        return None

    async def _y() -> None:
        return None

    ref_x = task("scoped_x")(_x)
    ref_y = task("scoped_y")(_y)

    with scoped_task_registration(ref_x):
        with scoped_task_registration(ref_y):
            assert get_broker().find_task("scoped_x") is not None
            assert get_broker().find_task("scoped_y") is not None
        # ref_y gone
        assert get_broker().find_task("scoped_y") is None
        assert get_broker().find_task("scoped_x") is not None
    # ref_x gone
    assert get_broker().find_task("scoped_x") is None
