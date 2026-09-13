"""Service-tier guard for the per-minute `subscriber_sweeper` `@scheduled` task.

Invariants:

  - The sweeper body is registered with the taskiq broker under the public
    task name (the `@scheduled` decorator wires the `@task` step).
  - The sweeper prunes stale memberships and leaves fresh entries untouched.
  - Incompatible stored records cannot prevent other keys from being swept.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from app.core.agent_gateway.subscribers import (
    _SUBSCRIBER_STALE_THRESHOLD_SECONDS,
    RunRouteFields,
    _run_subscriber_sweeper,
    subscriber_sweeper,
)
from app.core.cache import (
    count_members,
    delete_record,
    list_members,
    read_record,
    remove_member,
    touch_member,
    update_record,
)
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

    Seeds three members: two with timestamps past the stale threshold
    and one with the current timestamp. After running `_run_subscriber_sweeper`,
    only the fresh member should remain.
    """
    run_id = uuid4()
    key = f"run_subscribers:{run_id}"
    now = time.time()
    stale_ts = now - _SUBSCRIBER_STALE_THRESHOLD_SECONDS - 10

    stale_a = f"stale-pod-a:{uuid4()}"
    stale_b = f"stale-pod-b:{uuid4()}"
    fresh = f"fresh-pod:{uuid4()}"

    await touch_member(key, stale_a, last_seen=stale_ts)
    await touch_member(key, stale_b, last_seen=stale_ts - 5)
    await touch_member(key, fresh, last_seen=now)

    assert await count_members(key) == 3

    await _run_subscriber_sweeper()

    assert await count_members(key) == 1
    assert [(entry.member, entry.last_seen) for entry in await list_members(key)] == [(fresh, now)]

    # Cleanup.
    await remove_member(key, fresh)


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_removes_stale_agent_routes_and_keeps_fresh(redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """Sweeper also covers the literal agent_routes: prefix.

    Seeds two route memberships: one stale (beyond threshold)
    and one fresh (current timestamp). After running _run_subscriber_sweeper,
    only the fresh member should remain.
    """
    agent_id = uuid4()
    run_stale = str(uuid4())
    run_fresh = str(uuid4())
    key = f"agent_routes:{agent_id}"
    now = time.time()
    stale_ts = now - _SUBSCRIBER_STALE_THRESHOLD_SECONDS - 10

    await touch_member(key, run_stale, last_seen=stale_ts)
    await touch_member(key, run_fresh, last_seen=now)

    assert await count_members(key) == 2

    await _run_subscriber_sweeper()

    assert await count_members(key) == 1
    assert [(entry.member, entry.last_seen) for entry in await list_members(key)] == [(run_fresh, now)]

    # Cleanup.
    await remove_member(key, run_fresh)


@pytest.mark.parametrize("prefix", ["run_subscribers:", "agent_routes:"])
async def test_sweeper_isolates_incompatible_membership(redis_or_skip: None, prefix: str) -> None:
    """A record stored under a membership key cannot abort either namespace sweep."""
    invalid_key = f"{prefix}{uuid4()}"
    run_key = f"run_subscribers:{uuid4()}"
    routes_key = f"agent_routes:{uuid4()}"
    unrelated_key = f"unrelated:{uuid4()}"
    fields = RunRouteFields(workspace_id=uuid4(), agent_id=uuid4())
    now = time.time()
    stale = now - _SUBSCRIBER_STALE_THRESHOLD_SECONDS - 10
    await update_record(invalid_key, fields=fields)
    try:
        for key in (run_key, routes_key, unrelated_key):
            await touch_member(key, "stale", last_seen=stale)
            await touch_member(key, "fresh", last_seen=now)

        await _run_subscriber_sweeper()

        for key in (run_key, routes_key):
            assert [(entry.member, entry.last_seen) for entry in await list_members(key)] == [("fresh", now)]
        assert await read_record(invalid_key) == fields.model_dump(mode="json")
        assert await count_members(unrelated_key) == 2
    finally:
        await delete_record(invalid_key)
        for key in (run_key, routes_key, unrelated_key):
            await remove_member(key, "stale")
            await remove_member(key, "fresh")
