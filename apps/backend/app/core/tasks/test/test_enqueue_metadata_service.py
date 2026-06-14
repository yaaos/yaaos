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
from app.core.tasks import enqueue, task
from app.core.tasks.models import OutboxEntryRow
from app.core.tasks.service import scoped_task_registration


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
        meta = row.payload.get("metadata") or {}
        assert meta.get("org_id") == str(some_org), f"org_id mismatch: {meta}"
        # traceparent is auto-filled from the current span (None when no span is active).
        assert "traceparent" in meta, f"traceparent key missing from metadata: {meta}"


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
        meta = row.payload.get("metadata") or {}
        assert meta.get("org_id") == str(other_org), f"org_id mismatch: {meta}"
        assert "traceparent" in meta, f"traceparent key missing from metadata: {meta}"


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


def test_task_metadata_traceparent_roundtrip() -> None:
    """`TaskMetadata` serializes and deserializes traceparent correctly."""
    from app.core.tasks import TaskMetadata  # noqa: PLC0415

    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    orig = TaskMetadata(org_id=None, traceparent=tp)
    json_str = orig.model_dump_json()
    restored = TaskMetadata.model_validate_json(json_str)
    assert restored.traceparent == tp
    assert restored.org_id is None

    # Also verify dict round-trip (used by drain).
    as_dict = orig.model_dump(mode="json")
    from_dict = TaskMetadata.model_validate(as_dict)
    assert from_dict.traceparent == tp


@pytest.mark.asyncio
@pytest.mark.service
async def test_enqueue_autofills_traceparent(db_session) -> None:  # type: ignore[no-untyped-def]
    """`enqueue` stamps the current OTel traceparent into the outbox row's
    metadata when called inside an active span."""
    from opentelemetry import trace  # noqa: PLC0415

    from app.core.observability import current_traceparent  # noqa: PLC0415
    from app.core.tasks.models import OutboxEntryRow  # noqa: PLC0415
    from app.testing.observability import span_capture  # noqa: PLC0415

    # span_capture() ensures a real TracerProvider is installed globally so
    # current_traceparent() returns a non-None value inside an active span.
    # The captured spans themselves aren't inspected here — the assertion is
    # on the outbox row's metadata.traceparent.
    with span_capture():
        tracer = trace.get_tracer("test_autofill_tp")

        async def _task_d() -> None:
            return None

        ref = task("meta_autofill_tp")(_task_d)
        with scoped_task_registration(ref):
            with tracer.start_as_current_span("producer-span"):
                expected_tp = current_traceparent()
                assert expected_tp is not None, "must have an active span"
                await enqueue(ref, args={}, session=db_session)
                await db_session.commit()

            row = (
                await db_session.execute(
                    select(OutboxEntryRow).where(
                        OutboxEntryRow.payload["task_name"].astext == "meta_autofill_tp"
                    )
                )
            ).scalar_one()
            meta = row.payload.get("metadata") or {}
            assert meta.get("traceparent") == expected_tp, (
                f"expected traceparent={expected_tp!r}; got {meta.get('traceparent')!r}"
            )
