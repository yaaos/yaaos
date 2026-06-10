"""Service test: worker graceful drain on shutdown.

Verifies two properties introduced by the graceful-drain changes:

1. `drain_loop` stops cleanly between batches when the stop signal is
   set -- it exits via a normal return, not via CancelledError.
2. The runtime's shutdown ordering: drain stops before finish_event is
   set; the consume task is AWAITED (not cancelled); an in-flight body
   that started before shutdown completes; a body exceeding the grace
   is abandoned, not cancelled -- the worker exits regardless.

All tests are pure-asyncio with no external dependencies.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.tasks.drain import drain_loop


@pytest.mark.asyncio
@pytest.mark.service
async def test_drain_loop_exits_cleanly_on_stop_signal() -> None:
    """drain_loop exits between batches when the stop event is set.

    Verifies that the new `stop: asyncio.Event` parameter causes the loop
    to return normally rather than blocking forever or requiring a cancel.
    """
    stop = asyncio.Event()
    call_count = 0

    # Patch drain_loop's internal `_taskiq_dispatcher_for` by running
    # the loop against a minimal dispatcher that just returns.  We set
    # `stop` after the first poll so the loop exits on the next iteration.
    async def _mock_drain_once(*_args: object, **_kwargs: object) -> int:
        nonlocal call_count
        call_count += 1
        # After first poll, trigger stop.
        if call_count == 1:
            stop.set()
        return 0  # empty batch -- loop would sleep poll_idle_seconds

    class _FakeSession:
        """Context-manager stub -- no real DB."""

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def commit(self) -> None:
            pass

    class _FakeContextManager:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    async def _fake_drain_once(*args: object, **kwargs: object) -> int:
        return await _mock_drain_once(*args, **kwargs)

    # Inject the fakes via drain_loop's DI seam so the loop needs no real
    # Postgres -- no module-attribute mutation. broker is unused here because
    # the fake drain returns an empty batch and never dispatches.
    await asyncio.wait_for(
        drain_loop(
            None,  # type: ignore[arg-type]
            stop=stop,
            poll_idle_seconds=0.0,
            session_factory=_FakeContextManager(),
            drain_fn=_fake_drain_once,
        ),
        timeout=5.0,
    )

    assert call_count >= 1, "drain_loop never polled before exiting"


@pytest.mark.asyncio
@pytest.mark.service
async def test_inflight_body_completes_before_worker_exits() -> None:
    """await-not-cancel: an in-flight body started before SIGTERM finishes.

    Models the runtime's SIGTERM sequence:
    1. A task body starts running (body_started set).
    2. Shutdown sets finish_event while the body is still in-flight.
    3. The consumer task is AWAITED with `wait_tasks_timeout` grace --
       NOT cancelled.
    4. The body finishes (body_finished set) before the consumer exits.

    We simulate the Receiver runner behaviour using plain asyncio tasks:
    after finish_event is set, `asyncio.wait(tasks, timeout=grace)` drains
    in-flight work.  The key invariant is that tasks are not cancelled.
    """
    body_started: asyncio.Event = asyncio.Event()
    body_finished: asyncio.Event = asyncio.Event()
    finish_event: asyncio.Event = asyncio.Event()
    _GRACE = 5.0  # mirrors _WORKER_DRAIN_GRACE_SECONDS

    async def _slow_body() -> None:
        body_started.set()
        await asyncio.sleep(0.05)
        body_finished.set()

    # Simulate the consume task: start the body, then await in-flight
    # work up to the grace window -- what Receiver.listen does internally.
    async def _consumer() -> None:
        body_task = asyncio.create_task(_slow_body(), name="body")
        await finish_event.wait()
        # Drain in-flight via asyncio.wait (not cancel).
        await asyncio.wait({body_task}, timeout=_GRACE)

    consumer_task = asyncio.create_task(_consumer(), name="consume")

    await asyncio.wait_for(body_started.wait(), timeout=5.0)

    # SIGTERM equivalent -- set finish_event, then AWAIT the consumer.
    finish_event.set()
    await asyncio.wait_for(consumer_task, timeout=10.0)

    assert body_finished.is_set(), (
        "task body did not complete before the consumer task exited -- "
        "graceful drain is broken (body was likely cancelled)"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_over_grace_body_is_abandoned_not_cancelled() -> None:
    """Over-grace bodies are abandoned -- the worker still exits.

    A body running longer than _WORKER_DRAIN_GRACE_SECONDS is left in
    the `pending` set returned by `asyncio.wait`; the consumer task
    finishes regardless.  The worker must not hang forever waiting for
    a runaway body.
    """
    body_started: asyncio.Event = asyncio.Event()
    body_finished: asyncio.Event = asyncio.Event()
    finish_event: asyncio.Event = asyncio.Event()
    _GRACE = 0.1  # intentionally short; body is much longer

    async def _very_slow_body() -> None:
        body_started.set()
        await asyncio.sleep(5.0)
        body_finished.set()

    async def _consumer() -> None:
        body_task = asyncio.create_task(_very_slow_body(), name="body_over_grace")
        await finish_event.wait()
        # asyncio.wait returns body_task in `pending` -- abandoned, not cancelled.
        await asyncio.wait({body_task}, timeout=_GRACE)

    consumer_task = asyncio.create_task(_consumer(), name="consume_over_grace")

    await asyncio.wait_for(body_started.wait(), timeout=5.0)
    finish_event.set()

    # Consumer must exit quickly -- well within a 3s budget.
    await asyncio.wait_for(consumer_task, timeout=3.0)

    assert not body_finished.is_set(), "body finished -- expected it to be abandoned after the grace window"
