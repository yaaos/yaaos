"""Service-tier guard for the per-minute `subscriber_sweeper` `@scheduled` task.

Two invariants:

  - The sweeper body is registered with the taskiq broker under the public
    task name (the `@scheduled` decorator wires the `@task` step).
  - The sweeper body removes ZSET members whose score (unix timestamp) is
    older than `_SUBSCRIBER_STALE_THRESHOLD_SECONDS` and leaves fresh
    entries untouched.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from app.core.agent_gateway.subscribers import (
    _SUBSCRIBER_STALE_THRESHOLD_SECONDS,
    _run_subscriber_sweeper,
    subscriber_sweeper,
)
from app.core.redis import zset_add_member, zset_card, zset_remove_member
from app.core.tasks import get_broker

pytestmark = [pytest.mark.service, pytest.mark.asyncio]

_TASK_NAME = "subscriber_sweeper"


@pytest.mark.asyncio
@pytest.mark.service
async def test_subscriber_sweeper_registered_with_broker() -> None:
    """The sweeper body is registered with the taskiq broker under its
    task name. Regression guard for `@scheduled` decorator wiring."""
    _ = subscriber_sweeper  # ensure the decorated object is imported
    assert get_broker().find_task(_TASK_NAME) is not None


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_removes_stale_entries_and_keeps_fresh(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """Sweeper body removes members older than threshold, keeps fresh ones.

    Seeds three ZSET members: two with timestamps past the stale threshold
    and one with the current timestamp. After running `_run_subscriber_sweeper`,
    only the fresh member should remain.
    """
    wfx_id = uuid4()
    key = f"workflow_subscribers:{wfx_id}"
    now = time.time()
    stale_ts = now - _SUBSCRIBER_STALE_THRESHOLD_SECONDS - 10

    stale_a = f"stale-pod-a:{uuid4()}"
    stale_b = f"stale-pod-b:{uuid4()}"
    fresh = f"fresh-pod:{uuid4()}"

    await zset_add_member(key, stale_a, stale_ts)
    await zset_add_member(key, stale_b, stale_ts - 5)
    await zset_add_member(key, fresh, now)

    assert await zset_card(key) == 3

    await _run_subscriber_sweeper()

    card_after = await zset_card(key)
    assert card_after == 1, f"expected 1 fresh member to remain; got {card_after}"

    # Cleanup.
    await zset_remove_member(key, fresh)


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_removes_stale_agent_routes_and_keeps_fresh(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """Sweeper also covers agent_routes:* ZSETs.

    Seeds two members into an agent_routes ZSET: one stale (beyond threshold)
    and one fresh (current timestamp). After running _run_subscriber_sweeper,
    only the fresh member should remain.
    """
    agent_id = uuid4()
    wfx_stale = str(uuid4())
    wfx_fresh = str(uuid4())
    key = f"agent_routes:{agent_id}"
    now = time.time()
    stale_ts = now - _SUBSCRIBER_STALE_THRESHOLD_SECONDS - 10

    await zset_add_member(key, wfx_stale, stale_ts)
    await zset_add_member(key, wfx_fresh, now)

    assert await zset_card(key) == 2

    await _run_subscriber_sweeper()

    card_after = await zset_card(key)
    assert card_after == 1, f"expected 1 fresh member to remain in agent_routes; got {card_after}"

    # Cleanup.
    await zset_remove_member(key, wfx_fresh)
