"""Scheduler-loop error backoff — bounded exponential cadence.

Unit-tests the pure `_backoff_sleep` helper that the scheduler loop uses on
its exception path. Proves the sleep grows exponentially, caps at the module
ceiling, and that a successful tick (counter reset to 0) restores the normal
cadence.
"""

from __future__ import annotations

from app.core.tasks.scheduler import _MAX_SLEEP_SECONDS, _backoff_sleep


def test_backoff_grows_exponentially() -> None:
    tick = 20.0
    # First failure: 20 * 2**1 = 40; second: 80; third would be 160 -> capped.
    assert _backoff_sleep(1, tick) == 40.0
    assert _backoff_sleep(2, tick) == 80.0


def test_backoff_caps_at_max() -> None:
    tick = 20.0
    # 20 * 2**3 = 160 > 120 -> clamped to the ceiling, and stays there.
    assert _backoff_sleep(3, tick) == _MAX_SLEEP_SECONDS
    assert _backoff_sleep(10, tick) == _MAX_SLEEP_SECONDS


def test_backoff_schedule_is_monotonic_then_flat() -> None:
    tick = 20.0
    seq = [_backoff_sleep(n, tick) for n in range(1, 8)]
    # Non-decreasing and never exceeds the cap.
    assert seq == sorted(seq)
    assert all(s <= _MAX_SLEEP_SECONDS for s in seq)
    assert seq[-1] == _MAX_SLEEP_SECONDS


def test_reset_restores_normal_cadence() -> None:
    # A successful tick resets consecutive_failures to 0; the loop then sleeps
    # the plain tick_interval, NOT a backoff value. The first failure after a
    # reset starts the ramp again at 2x.
    tick = 20.0
    assert _backoff_sleep(1, tick) == 40.0  # first failure after reset


def test_respects_custom_tick_interval() -> None:
    # Tests run with a short tick; backoff must scale off the given interval.
    assert _backoff_sleep(1, 1.0) == 2.0
    assert _backoff_sleep(2, 1.0) == 4.0
    assert _backoff_sleep(1, 1.0, max_sleep=3.0) == 2.0
    assert _backoff_sleep(2, 1.0, max_sleep=3.0) == 3.0
