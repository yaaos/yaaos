"""Isolation fixtures for service tests.

Provides pytest fixtures that reset per-module singletons to a clean state
before each test. All resets are performed by calling each module's production
registration/deregistration APIs — no direct submodule attribute access.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest_asyncio

from app.core.redis import RedisPubsub, bind_pubsub
from app.domain.vcs import (
    get_plugin,
    is_registered,
    register_vcs_plugin,
    unregister_vcs_plugin,
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
async def agent_queues_isolation() -> None:
    """Bind a fresh AgentQueues instance for each test.

    Autouse so every test gets an isolated, empty dispatch-queue registry
    without importing or calling anything.
    """
    from app.core.agent_gateway import AgentQueues, bind_agent_queues  # noqa: PLC0415

    bind_agent_queues(AgentQueues())


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
    """Clear the workspace-provider registry before and after the test.

    Non-autouse: tests that register custom workspace providers must
    request this fixture to ensure isolation. Uses the public
    list/unregister API to clear without needing a private clear function.
    """
    from app.core.workspace import (  # noqa: PLC0415
        list_workspace_providers,
        unregister_workspace_provider,
    )

    def _clear() -> None:
        for p in list_workspace_providers():
            unregister_workspace_provider(p.meta.id)

    _clear()
    yield
    _clear()


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
    restore the prior entry (if any) on exit — even if an exception is raised.

    If *plugin.meta.id* is already registered the prior entry is saved and
    replaced; on exit the prior entry is restored. If the id was not
    registered the plugin is simply unregistered on exit.

    Uses only the public VCS registry API (register/unregister/is_registered).
    """
    plugin_id = plugin.meta.id
    prior = get_plugin(plugin_id) if is_registered(plugin_id) else None
    if prior is not None:
        unregister_vcs_plugin(plugin_id)
    register_vcs_plugin(plugin)
    try:
        yield plugin
    finally:
        unregister_vcs_plugin(plugin_id)
        if prior is not None:
            register_vcs_plugin(prior)


__all__ = [
    "agent_queues_isolation",
    "email_inbox_isolation",
    "pubsub_isolation",
    "recovery_policies_isolation",
    "scoped_vcs_plugin",
    "subscriber_registry_isolation",
    "workflow_context_provider_isolation",
    "workspace_providers_isolation",
]
