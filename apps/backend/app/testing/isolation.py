"""Isolation fixtures for service tests.

Provides pytest fixtures that reset per-module singletons to a clean state
before each test. All resets are performed by calling each module's production
registration/deregistration APIs — no direct submodule attribute access.
"""

from __future__ import annotations

import pytest_asyncio

from app.core.redis import RedisPubsub, bind_pubsub


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
    sent emails import `read_email_inbox` from `app.testing.isolation`.
    """
    from app.domain.orgs.email import _Inbox, bind_email_inbox  # noqa: PLC0415

    bind_email_inbox(_Inbox())


def read_email_inbox() -> list:
    """Return the list of `SentEmail` items captured in the current test's inbox.

    The list is mutable — tests may call `.clear()` on it if they need to
    discard prior messages within a single test body.
    """
    from app.domain.orgs.email import get_email_inbox  # noqa: PLC0415

    return get_email_inbox().messages


__all__ = [
    "agent_queues_isolation",
    "email_inbox_isolation",
    "pubsub_isolation",
    "read_email_inbox",
    "subscriber_registry_isolation",
]
