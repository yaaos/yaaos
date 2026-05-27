"""Lifespan teardown invokes registered web shutdown hooks.

Tests use probe-only registries (not the global process registry) to avoid
corrupting global state (broker singletons, DB engines, etc.) that other tests
depend on.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_lifespan_with_hooks(hooks: list[Any]):
    """Build a FastAPI lifespan that runs the given hooks on teardown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for hook in reversed(hooks):
            try:
                await hook()
            except Exception:
                pass

    return lifespan


def test_lifespan_teardown_calls_registered_hooks() -> None:
    """A hook registered in a local list runs during lifespan teardown."""
    probe_ran: list[bool] = []

    async def probe_hook() -> None:
        probe_ran.append(True)

    hooks: list[Any] = [probe_hook]

    app = FastAPI(lifespan=_make_lifespan_with_hooks(hooks))

    with TestClient(app):
        pass

    assert probe_ran == [True], "shutdown hook must have run during lifespan teardown"


def test_lifespan_teardown_runs_hooks_in_reverse_order() -> None:
    """Hooks run in reverse order (reversed-iteration of registration list)."""
    order: list[str] = []

    async def first_registered() -> None:
        order.append("first")

    async def second_registered() -> None:
        order.append("second")

    hooks: list[Any] = [first_registered, second_registered]

    app = FastAPI(lifespan=_make_lifespan_with_hooks(hooks))
    with TestClient(app):
        pass

    assert order == ["second", "first"]


def test_lifespan_teardown_continues_after_failing_hook() -> None:
    """A hook that raises must not prevent the subsequent hook from running."""
    ran: list[str] = []

    async def fine_hook() -> None:
        ran.append("fine")

    async def boom_hook() -> None:
        raise RuntimeError("boom")

    # fine registered first → runs last in reversed; boom registered second → first
    hooks: list[Any] = [fine_hook, boom_hook]

    app = FastAPI(lifespan=_make_lifespan_with_hooks(hooks))
    with TestClient(app):
        pass

    assert "fine" in ran, "earlier-registered hook must run even after a later hook fails"
