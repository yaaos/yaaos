"""Daily prune of the `scheduled_runs` dedup ledger.

The exactly-once claim row is meaningful only for the slot it gates; once
the slot is past nothing will ever re-claim it. We keep 7 days as an
operator-visible audit window (which slots fired, by which worker — the
`created_at` column carries that), then prune.

This is the first `@scheduled` consumer in the codebase — self-exercising
the scheduler + claim machinery in production. Body is idempotent
(deleting an already-deleted row is a no-op).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete

from app.core.database import session as db_session
from app.core.tasks.models import ScheduledRunRow
from app.core.tasks.scheduler import scheduled

log = structlog.get_logger("core.tasks.scheduled_runs_prune")

_RETENTION = timedelta(days=7)


async def prune_scheduled_runs() -> None:
    """Delete `scheduled_runs` rows older than 7 days. Idempotent.

    Module-public so service tests can invoke the body directly without
    going through the broker dispatch path."""
    cutoff = datetime.now(UTC) - _RETENTION
    async with db_session() as s:
        result = await s.execute(delete(ScheduledRunRow).where(ScheduledRunRow.created_at < cutoff))
        await s.commit()
    log.info("tasks.scheduled_runs_prune.done", deleted=result.rowcount)


# Register the prune body with the scheduler as the named `@scheduled`
# task. Runs daily at midnight UTC.
scheduled_runs_prune = scheduled(
    name="scheduled_runs_prune", cron="0 0 * * *", queue="default", max_retries=1
)(prune_scheduled_runs)
