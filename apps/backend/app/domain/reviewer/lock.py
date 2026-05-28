"""Per-PR PG advisory lock.

`pg_advisory_xact_lock(hashtext('pr:' || pr_id::text)::bigint)` at the start
of every aggregate-mutating transaction. Released automatically at transaction
end. Two webhook events for the same PR serialize cleanly.

Read-only entry points (`list_*`, `get_*`) do not take this lock.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def acquire_pr_lock(session: AsyncSession, pr_id: uuid.UUID) -> None:
    """Take the per-PR advisory lock for the current transaction.

    Idempotent within a transaction — Postgres tracks per-(xid, lock-key)
    acquisitions but only releases at commit/rollback. Calling twice from
    nested helpers is harmless.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"pr:{pr_id}"},
    )
