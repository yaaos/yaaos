"""`database.ping()` runs its query under suppressed instrumentation.

The health pings (web `/api/health` + worker `/health`) run a `SELECT 1` on
every probe. Left instrumented, each probe produces a SQLAlchemy span — pure
noise. `ping()` wraps the query in `suppress_instrumentation()`. A
`before_cursor_execute` listener records `is_instrumentation_enabled()` at the
moment the statement runs: it must be False inside `ping()` and True for an
ordinary query (the control proves the probe is wired).
"""

from __future__ import annotations

import pytest
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core import database


@pytest.mark.asyncio
@pytest.mark.service
async def test_ping_query_runs_with_instrumentation_suppressed(db_session) -> None:  # type: ignore[no-untyped-def]
    observed: list[bool] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        observed.append(is_instrumentation_enabled())

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        assert await database.ping() is True
        assert observed, "ping() should have issued at least one query"
        assert all(enabled is False for enabled in observed), (
            "ping() must run its query inside suppress_instrumentation()"
        )

        # Control: an ordinary query runs with instrumentation enabled, proving
        # the listener observes the real flag rather than always seeing False.
        observed.clear()
        await db_session.execute(text("SELECT 1"))
        assert observed and all(enabled is True for enabled in observed), (
            "a non-suppressed query should run with instrumentation enabled"
        )
    finally:
        event.remove(Engine, "before_cursor_execute", _record)
