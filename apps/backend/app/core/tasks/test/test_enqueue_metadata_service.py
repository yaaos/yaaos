"""Service tests: enqueue `metadata` kwarg — auto-fill from contextvar + explicit override.

Three scenarios:
1. Auto-fill from contextvar when `metadata` is omitted but `org_id_var` is set.
2. Explicit `metadata` overrides the contextvar.
3. No contextvar + no explicit `metadata` → outbox row carries no `metadata`
   (guards the system-bootstrap path where tasks run outside any org context).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.tasks import enqueue, scoped_task_registration, task
from app.core.tasks.models import OutboxEntryRow


@pytest.mark.asyncio
@pytest.mark.service
async def test_enqueue_auto_fills_org_id_from_contextvar(db_session) -> None:  # type: ignore[no-untyped-def]
    """When `metadata` is omitted and the `org_id` contextvar is set, `enqueue`
    writes `{"org_id": str(org_id)}` into the outbox row's payload metadata."""
    some_org = uuid4()

    async def _task_a() -> None:
        return None

    ref = task("meta_auto_fill")(_task_a)
    with scoped_task_registration(ref):
        async with org_context(some_org, ActorKind.USER):
            await enqueue(ref, args={}, session=db_session)
            await db_session.commit()

        row = (
            await db_session.execute(
                select(OutboxEntryRow).where(OutboxEntryRow.payload["task_name"].astext == "meta_auto_fill")
            )
        ).scalar_one()
        assert row.payload.get("metadata") == {"org_id": str(some_org)}


@pytest.mark.asyncio
@pytest.mark.service
async def test_enqueue_explicit_metadata_overrides_contextvar(db_session) -> None:  # type: ignore[no-untyped-def]
    """Explicit `metadata` kwarg wins over the contextvar value."""
    contextvar_org = uuid4()
    other_org = uuid4()

    async def _task_b() -> None:
        return None

    ref = task("meta_explicit_override")(_task_b)
    with scoped_task_registration(ref):
        async with org_context(contextvar_org, ActorKind.USER):
            await enqueue(
                ref,
                args={},
                metadata={"org_id": str(other_org)},
                session=db_session,
            )
            await db_session.commit()

        row = (
            await db_session.execute(
                select(OutboxEntryRow).where(
                    OutboxEntryRow.payload["task_name"].astext == "meta_explicit_override"
                )
            )
        ).scalar_one()
        assert row.payload.get("metadata") == {"org_id": str(other_org)}


@pytest.mark.asyncio
@pytest.mark.service
async def test_enqueue_with_no_contextvar_and_no_metadata_leaves_metadata_empty(db_session) -> None:  # type: ignore[no-untyped-def]
    """Outside any org_context and with no explicit `metadata`, the outbox row
    carries no `metadata` key (or `None`). Guards the system-bootstrap path."""
    from app.core.auth import current_org_id  # noqa: PLC0415

    # Guard: ensure we really are outside any org context.
    assert current_org_id() is None, "test must run outside any org_context"

    async def _task_c() -> None:
        return None

    ref = task("meta_no_context")(_task_c)
    with scoped_task_registration(ref):
        await enqueue(ref, args={}, session=db_session)
        await db_session.commit()

        row = (
            await db_session.execute(
                select(OutboxEntryRow).where(OutboxEntryRow.payload["task_name"].astext == "meta_no_context")
            )
        ).scalar_one()
        # metadata should be absent or None — no auto-fill, no crash.
        assert row.payload.get("metadata") is None
