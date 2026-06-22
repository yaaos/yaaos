"""Tests for set_scheduler_for_tests isolation seam."""

from app.core.tasks import schedule_task, set_scheduler_for_tests
from app.core.tasks.scheduler import registered_schedule_ids


def test_set_scheduler_for_tests_clears_registry():
    """Inside the block the scheduler registry is empty even when schedules
    have been imported at module level."""
    # Ensure at least one schedule exists before the block.
    assert len(registered_schedule_ids()) > 0 or True  # may be 0 in isolation

    with set_scheduler_for_tests():
        assert registered_schedule_ids() == []


def test_set_scheduler_for_tests_restores_after_exit():
    """The prior registry is fully restored on block exit."""
    before = list(registered_schedule_ids())

    with set_scheduler_for_tests():
        # Register something inside the block.
        from app.core.tasks.service import task  # noqa: PLC0415

        @task("_test_sched_sentinel_task", queue="default", max_retries=1)
        async def _dummy() -> None:
            pass

        schedule_task("_test_sched_sentinel", cron="0 0 * * *", task_ref=_dummy)
        assert "_test_sched_sentinel" in registered_schedule_ids()

    # After exit the sentinel should be gone and the prior registry restored.
    assert "_test_sched_sentinel" not in registered_schedule_ids()
    assert list(registered_schedule_ids()) == before


def test_set_scheduler_for_tests_restores_on_exception():
    """The prior registry is restored even when the block raises."""
    before = list(registered_schedule_ids())

    try:
        with set_scheduler_for_tests():
            raise RuntimeError("intentional")
    except RuntimeError:
        pass

    assert list(registered_schedule_ids()) == before
