"""Daily rolling create-ahead + drop maintenance for `coding_agent_activity`.

The migration that introduces the partitioned table seeds a fixed
three-week window (prev/current/next ISO weeks). This scheduled task
keeps the window rolling: it creates partitions for the current week
plus the next two, and drops partitions older than four weeks (the
table's documented retention).

Raw partition DDL is owned by `core/database` (the only module the
table-access checker allows raw SQL against the `coding_agent_activity`
parent). This module's responsibility is purely scheduling — the body
delegates to `core.database.maintain_coding_agent_activity_partitions`.
Idempotent under repeat fire: `CREATE TABLE IF NOT EXISTS` for create,
`DROP TABLE IF EXISTS` for drop.
"""

from __future__ import annotations

import structlog

from app.core.database import maintain_coding_agent_activity_partitions
from app.core.tasks import scheduled

log = structlog.get_logger("core.coding_agent.partition_maintenance")


async def maintain_partitions() -> None:
    """Body of the daily `coding_agent_activity_partition_maintenance`
    `@scheduled` task. Idempotent; safe to invoke twice in the same day.

    Module-public so service tests can invoke the body directly without
    going through the broker dispatch path.
    """
    await maintain_coding_agent_activity_partitions()
    log.info("coding_agent_activity.partition_maintenance.done")


# Daily at 01:00 UTC — well clear of the midnight scheduled_runs prune so
# the two long-running maintenance bodies don't pile onto the same slot.
coding_agent_activity_partition_maintenance = scheduled(
    name="coding_agent_activity_partition_maintenance",
    cron="0 1 * * *",
    queue="default",
    max_retries=1,
)(maintain_partitions)
