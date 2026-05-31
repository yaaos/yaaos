"""Isolation fixtures for service tests.

Provides pytest fixtures that reset per-module singletons to a clean state
before each test. All resets are performed by binding a fresh instance per
test context — no direct submodule attribute mutation, no restore loops.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio

from app.core.redis import RedisPubsub, bind_pubsub
from app.domain.vcs import (
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
async def subscriber_registry_isolation() -> None:
    """Bind a fresh SubscriberRegistry instance for each test.

    Autouse so every test gets an isolated subscriber registry
    without importing or calling anything.
    """
    from app.core.agent_gateway import SubscriberRegistry, bind_subscriber_registry  # noqa: PLC0415

    bind_subscriber_registry(SubscriberRegistry())


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
    import os  # noqa: PLC0415

    import app.plugins.claude_code  # noqa: PLC0415
    import app.plugins.github  # noqa: F401, PLC0415

    if os.environ.get("YAAOS_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
        from app.testing.stub_coding_agent import wrap_all_registered_plugins  # noqa: PLC0415
        from app.testing.stub_workspace import wrap_all_registered_workspace_providers  # noqa: PLC0415

        wrap_all_registered_plugins()
        wrap_all_registered_workspace_providers()

    from app.core.workspace import current_workspace_registry  # noqa: PLC0415
    from app.domain.coding_agent import current_coding_agent_registry  # noqa: PLC0415

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
    from app.core.workspace import bind_workspace_registry  # noqa: PLC0415
    from app.domain.coding_agent import bind_coding_agent_registry  # noqa: PLC0415

    bind_coding_agent_registry(_canonical_registries.coding_agent.copy())
    bind_vcs_registry(_canonical_registries.vcs.copy())
    bind_workspace_registry(_canonical_registries.workspace.copy())


@pytest_asyncio.fixture
async def workflow_context_provider_isolation():
    """Reset the workflow-context provider before and after the test.

    Non-autouse: tests that need to control the registered provider (or
    assert on its absence) must request this fixture explicitly. Tests
    that just need a working provider should register one via
    `register_workflow_context_provider` after requesting this fixture.
    """
    from app.core.workspace.workflow_context import (  # noqa: PLC0415
        _clear_workflow_context_provider_for_tests,
    )

    _clear_workflow_context_provider_for_tests()
    yield
    _clear_workflow_context_provider_for_tests()


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


@pytest_asyncio.fixture
async def recovery_policies_isolation():
    """Clear all recovery policies before and after the test.

    Non-autouse: tests that register custom recovery policies must
    request this fixture. Tests that rely on the default
    `auth_expired → RefreshWorkspaceAuth` policy should call
    `register_workspace_recovery_policies()` after requesting this
    fixture to explicitly install it.
    """
    from app.core.workflow.recovery import _clear_recovery_policies_for_tests  # noqa: PLC0415

    _clear_recovery_policies_for_tests()
    yield
    _clear_recovery_policies_for_tests()


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
    "recovery_policies_isolation",
    "scoped_vcs_plugin",
    "subscriber_registry_isolation",
    "workflow_context_provider_isolation",
    "workspace_providers_isolation",
]
