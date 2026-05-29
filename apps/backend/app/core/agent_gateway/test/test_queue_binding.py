"""Service-level coverage of the ContextVar-based agent-queues and subscriber-registry binding."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    AgentQueues,
    AuthBlock,
    CreateWorkspaceCommand,
    RepoRef,
    SubscriberRegistry,
    bind_agent_queues,
    bind_subscriber_registry,
    claim_next,
    enqueue_command,
    get_subscriber_registry,
    queue_depth,
)
from app.core.agent_gateway.service import get_agent_queues


def _make_create_command() -> CreateWorkspaceCommand:
    return CreateWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=uuid4(),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )


# ── AgentQueues binding ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_agent_queues_produces_isolated_registry() -> None:
    """Binding a fresh AgentQueues instance gives an empty queue,
    isolated from any prior test's state."""
    agent = uuid4()
    # The autouse fixture already bound a fresh instance. Verify it's empty.
    assert queue_depth(agent) == 0


@pytest.mark.asyncio
async def test_enqueue_visible_in_bound_registry() -> None:
    """Enqueued command is visible via queue_depth on the same bound instance."""
    agent = uuid4()
    cmd = _make_create_command()
    await enqueue_command(agent, cmd)
    assert queue_depth(agent) == 1


@pytest.mark.asyncio
async def test_fresh_bind_hides_prior_enqueue() -> None:
    """Binding a new AgentQueues instance after an enqueue hides the prior state —
    isolation works across explicit re-binds within a test."""
    agent = uuid4()
    cmd = _make_create_command()
    await enqueue_command(agent, cmd)
    assert queue_depth(agent) == 1

    # Rebind to a fresh instance — state is gone.
    bind_agent_queues(AgentQueues())
    assert queue_depth(agent) == 0


@pytest.mark.asyncio
async def test_get_agent_queues_raises_before_bind() -> None:
    """Deliberately unbind and verify the fail-fast RuntimeError is raised."""
    from app.core.agent_gateway.service import _agent_queues_var  # noqa: PLC0415

    token = _agent_queues_var.set(None)
    try:
        with pytest.raises(RuntimeError, match="agent queues not bound"):
            get_agent_queues()
    finally:
        # Restore a bound instance so subsequent fixtures are unaffected.
        _agent_queues_var.reset(token)
        bind_agent_queues(AgentQueues())


@pytest.mark.asyncio
async def test_claim_returns_command_from_bound_registry() -> None:
    """claim_next drains from the currently-bound registry."""
    agent = uuid4()
    cmd = _make_create_command()
    await enqueue_command(agent, cmd)
    claimed = await claim_next(agent, wait_seconds=0)
    assert claimed is cmd
    assert queue_depth(agent) == 0


# ── SubscriberRegistry binding ─────────────────────────────────────────


def test_bind_subscriber_registry_produces_fresh_registry() -> None:
    """After the autouse fixture binds a fresh registry, there are no senders."""
    registry = get_subscriber_registry()
    # A freshly-bound registry has no senders for an arbitrary agent_id.
    assert not registry.has_sender(uuid4())


def test_fresh_bind_hides_prior_registry_state() -> None:
    """Binding a new SubscriberRegistry after mutations hides prior state."""
    # Inject a dummy agent_id into the current registry's internal structure.
    registry = get_subscriber_registry()
    fake_agent = uuid4()
    registry._senders[fake_agent] = lambda _: None  # type: ignore[assignment]
    assert registry.has_sender(fake_agent)

    # Rebind to a fresh registry — state is gone.
    bind_subscriber_registry(SubscriberRegistry())
    assert not get_subscriber_registry().has_sender(fake_agent)


def test_get_registry_raises_before_bind() -> None:
    """Deliberately unbind and verify the fail-fast RuntimeError fires."""
    from app.core.agent_gateway.subscribers import _registry_var  # noqa: PLC0415

    token = _registry_var.set(None)
    try:
        with pytest.raises(RuntimeError, match="subscriber registry not bound"):
            get_subscriber_registry()
    finally:
        _registry_var.reset(token)
        bind_subscriber_registry(SubscriberRegistry())
