"""drain_once is accessible from the package top-level."""

from __future__ import annotations

import pytest

from app.core.tasks import drain_once
from app.core.tasks.drain import write


@pytest.mark.asyncio
@pytest.mark.service
async def test_drain_once_public_import_works(db_session) -> None:  # type: ignore[no-untyped-def]
    """Importing drain_once from app.core.tasks works identically to the submodule."""
    await write(db_session, kind="taskiq_enqueue", payload={"x": 99})
    await db_session.commit()

    delivered: list[dict] = []

    async def _dispatcher(kind: str, payload: dict) -> None:
        delivered.append(payload)

    n = await drain_once(db_session, dispatcher=_dispatcher)
    await db_session.commit()

    assert n == 1
    assert delivered[0]["x"] == 99
