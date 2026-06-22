"""Isolation fixtures for service tests.

Provides pytest fixtures that reset per-module singletons to a clean state
before each test. All resets are performed by binding a fresh instance via
the module's `set_X_for_tests` context manager — no direct submodule attribute
mutation, no restore loops.
"""

from __future__ import annotations

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def pubsub_isolation():
    """Bind a fresh RedisPubsub instance for each test.

    Autouse so every test in the backend suite gets an isolated pubsub
    without importing or calling anything. Tests that depend on Redis
    still use the `redis_or_skip` fixture to gate on reachability.
    """
    from app.core.redis import set_pubsub_for_tests  # noqa: PLC0415

    with set_pubsub_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def bearer_verify_isolation():
    """Reset the agent-gateway bearer-verify override before each test.

    The override is ContextVar-bound; tests that need a stub call
    `with set_bearer_verify_for_tests(verify=stub):` inside the test body.
    """
    from app.core.agent_gateway import set_bearer_verify_for_tests  # noqa: PLC0415

    with set_bearer_verify_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def sts_verify_isolation():
    """Reset the STS identity-verify override before each test.

    The override is ContextVar-bound; tests that need a stub call
    `with set_sts_verify_for_tests(callback):` inside the test body.
    """
    from app.core.agent_gateway import set_sts_verify_for_tests  # noqa: PLC0415

    with set_sts_verify_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def sse_shutdown_event_isolation():
    """Bind a fresh asyncio.Event as the SSE shutdown signal for each test.

    Autouse so every test in the backend suite gets an isolated event without
    importing or calling anything. A previously-set event from another test
    cannot leak into this one.
    """
    from app.core.sse import set_shutdown_event_for_tests  # noqa: PLC0415

    with set_shutdown_event_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def subscriber_registry_isolation():
    """Bind a fresh SubscriberRegistry instance for each test.

    Autouse so every test gets an isolated subscriber registry
    without importing or calling anything.
    """
    from app.core.agent_gateway import set_subscriber_registry_for_tests  # noqa: PLC0415

    with set_subscriber_registry_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def scheduler_registry_isolation():
    """Clear the recurring-task scheduler registry per test.

    Schedules are registered at import time (e.g. the `scheduled_runs`
    prune); tests that register their own schedules (via `@scheduled`
    or `schedule_task`) must start with a known-empty registry so the
    `tick_once` call only fires the schedules the test introduced.
    """
    from app.core.tasks import set_scheduler_for_tests  # noqa: PLC0415

    with set_scheduler_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def email_inbox_isolation():
    """Bind a fresh email inbox for each test.

    Autouse so every test gets an isolated inbox. Tests that need to read
    sent emails call `read_sent_emails()` from `app.domain.orgs`.
    """
    from app.domain.orgs import set_email_inbox_for_tests  # noqa: PLC0415

    with set_email_inbox_for_tests():
        yield


@pytest_asyncio.fixture(autouse=True)
async def plugin_registries_isolation():
    """Bind a fresh copy of each plugin registry per test.

    Ensures plugins are imported and stub-wrapped (idempotent), then
    copies the stub-enriched defaults for per-test isolation. A test that
    registers or swaps a plugin only affects its own copy; the next test
    rebinds from the default — no restore, no leak, no order dependence.
    """
    import app.plugins.claude_code  # noqa: PLC0415
    import app.plugins.github  # noqa: PLC0415, F401
    from app.core.coding_agent import set_coding_agents_for_tests  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.vcs import set_vcs_for_tests  # noqa: PLC0415
    from app.core.workspace import set_workspace_providers_for_tests  # noqa: PLC0415

    if get_settings().yaaos_coding_agent_stub:
        from app.testing.stub_coding_agent import wrap_all_registered_plugins  # noqa: PLC0415
        from app.testing.stub_workspace import wrap_all_registered_workspace_providers  # noqa: PLC0415

        wrap_all_registered_plugins()
        wrap_all_registered_workspace_providers()

    with set_coding_agents_for_tests():
        with set_vcs_for_tests():
            with set_workspace_providers_for_tests():
                yield


@pytest_asyncio.fixture
async def workspace_providers_isolation():
    """Bind an empty workspace-provider registry before the test.

    Non-autouse: tests that register custom workspace providers must
    request this fixture to ensure isolation — the autouse
    `plugin_registries_isolation` restores the default next test regardless,
    but this fixture lets the test start from an explicitly empty registry.
    """
    from app.core.workspace import set_workspace_providers_for_tests  # noqa: PLC0415

    with set_workspace_providers_for_tests(scenario="empty"):
        yield


__all__ = [
    "bearer_verify_isolation",
    "email_inbox_isolation",
    "plugin_registries_isolation",
    "pubsub_isolation",
    "scheduler_registry_isolation",
    "sse_shutdown_event_isolation",
    "sts_verify_isolation",
    "subscriber_registry_isolation",
    "workspace_providers_isolation",
]
