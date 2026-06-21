"""Isolation fixtures for service tests.

Provides pytest fixtures that reset per-module singletons to a clean state
before each test. All resets are performed by binding a fresh instance per
test context — no direct submodule attribute mutation, no restore loops.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio

from app.core.redis import RedisPubsub, bind_pubsub
from app.core.sse import bind_shutdown_event
from app.core.vcs import (
    bind_vcs_registry,
    current_vcs_registry,
)


@pytest_asyncio.fixture(autouse=True)
async def pubsub_isolation() -> None:
    """Bind a fresh RedisPubsub instance for each test.

    Autouse so every test in the backend suite gets an isolated pubsub
    without importing or calling anything. Tests that depend on Redis
    still use the `redis_or_skip` fixture to gate on reachability.
    """
    bind_pubsub(RedisPubsub())


@pytest_asyncio.fixture(autouse=True)
async def bearer_verify_isolation() -> None:
    """Reset the agent-gateway bearer-verify override before each test.

    The override is ContextVar-bound; tests that need a stub call
    `bearers.set_verify_override(stub)` inside the test body and never
    have to restore it themselves.
    """
    from app.core.agent_gateway import bearers  # noqa: PLC0415

    bearers.set_verify_override(None)


@pytest_asyncio.fixture(autouse=True)
async def sse_shutdown_event_isolation() -> None:
    """Bind a fresh asyncio.Event as the SSE shutdown signal for each test.

    Autouse so every test in the backend suite gets an isolated event without
    importing or calling anything. A previously-set event from another test
    cannot leak into this one.
    """
    bind_shutdown_event(asyncio.Event())


@pytest_asyncio.fixture(autouse=True)
async def subscriber_registry_isolation() -> None:
    """Bind a fresh SubscriberRegistry instance for each test.

    Autouse so every test gets an isolated subscriber registry
    without importing or calling anything.
    """
    from app.core.agent_gateway import SubscriberRegistry, bind_subscriber_registry  # noqa: PLC0415

    bind_subscriber_registry(SubscriberRegistry())


@pytest_asyncio.fixture(autouse=True)
async def scheduler_registry_isolation() -> None:
    """Clear the recurring-task scheduler registry per test.

    Schedules are registered at import time (e.g. the `scheduled_runs`
    prune); tests that register their own schedules (via `@scheduled`
    or `schedule_task`) must start with a known-empty registry so the
    `tick_once` call only fires the schedules the test introduced.
    """
    from app.core.tasks.scheduler import _reset_schedules_for_tests  # noqa: PLC0415

    _reset_schedules_for_tests()


@pytest_asyncio.fixture(autouse=True)
async def email_inbox_isolation() -> None:
    """Bind a fresh email inbox for each test.

    Autouse so every test gets an isolated inbox. Tests that need to read
    sent emails import `read_email_inbox` from `app.testing.seed`.
    """
    from app.domain.orgs.email import _Inbox, bind_email_inbox  # noqa: PLC0415

    bind_email_inbox(_Inbox())


@pytest.fixture(scope="session")
def _canonical_registries():
    """Build canonical plugin registry snapshots once per session.

    Imports the three plugin packages (triggering their import-time
    bootstrap), then optionally wraps with stubs when YAAOS_CODING_AGENT_STUB
    is set, and returns snapshots via .copy(). Never calls bootstrap() again —
    the import handles it.
    """
    import app.plugins.claude_code  # noqa: PLC0415
    import app.plugins.github  # noqa: F401, PLC0415
    from app.core.config import get_settings  # noqa: PLC0415

    if get_settings().yaaos_coding_agent_stub:
        from app.testing.stub_coding_agent import wrap_all_registered_plugins  # noqa: PLC0415
        from app.testing.stub_workspace import wrap_all_registered_workspace_providers  # noqa: PLC0415

        wrap_all_registered_plugins()
        wrap_all_registered_workspace_providers()

    from app.core.coding_agent import current_coding_agent_registry  # noqa: PLC0415
    from app.core.workspace import current_workspace_registry  # noqa: PLC0415

    class _Snapshot:
        coding_agent = current_coding_agent_registry().copy()
        vcs = current_vcs_registry().copy()
        workspace = current_workspace_registry().copy()

    return _Snapshot()


@pytest_asyncio.fixture(autouse=True)
async def plugin_registries_isolation(_canonical_registries) -> None:
    """Bind a fresh copy of each canonical plugin registry per test.

    Unconditional + autouse. A test that registers or swaps a plugin only
    affects its own copy; the next test rebinds from the canonical snapshot —
    no restore, no leak, no order dependence.
    """
    from app.core.coding_agent import bind_coding_agent_registry  # noqa: PLC0415
    from app.core.workspace import bind_workspace_registry  # noqa: PLC0415

    bind_coding_agent_registry(_canonical_registries.coding_agent.copy())
    bind_vcs_registry(_canonical_registries.vcs.copy())
    bind_workspace_registry(_canonical_registries.workspace.copy())


@pytest_asyncio.fixture
async def workspace_providers_isolation():
    """Bind an empty workspace-provider registry before the test, then
    restore the canonical binding on exit.

    Non-autouse: tests that register custom workspace providers must
    request this fixture to ensure isolation — the autouse
    `plugin_registries_isolation` restores canonical next test regardless,
    but this fixture lets the test start from an explicitly empty registry.
    """
    from app.core.workspace import WorkspaceRegistry, bind_workspace_registry  # noqa: PLC0415

    bind_workspace_registry(WorkspaceRegistry())
    yield


@contextmanager
def scoped_vcs_plugin(plugin) -> Iterator:  # type: ignore[type-arg]
    """Context manager: install *plugin* for the duration of the block, then
    restore the prior registry binding on exit — even if an exception is raised.

    Binds a fresh registry copy with the plugin inserted; restores the prior
    binding on exit. Never mutates the canonical registry dict.
    """
    prior = current_vcs_registry()
    fresh = prior.copy()
    fresh.replace(plugin)  # type: ignore[arg-type]
    bind_vcs_registry(fresh)
    try:
        yield plugin
    finally:
        bind_vcs_registry(prior)


__all__ = [
    "email_inbox_isolation",
    "plugin_registries_isolation",
    "pubsub_isolation",
    "scheduler_registry_isolation",
    "scoped_vcs_plugin",
    "sse_shutdown_event_isolation",
    "subscriber_registry_isolation",
    "workspace_providers_isolation",
]
