"""Service test: org_context middleware sets current_org_id inside task bodies.

Verifies that when a task is enqueued with metadata={"org_id": ...}, the
OrgContextMiddleware enters org_context before the task body runs, so
current_org_id() returns the expected UUID inside the body.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from taskiq import InMemoryBroker

from app.core.auth import current_org_id
from app.core.tasks import drain_once, enqueue, scoped_task_registration, task
from app.core.tasks.drain import _taskiq_dispatcher_for
from app.core.tasks.middleware import OrgContextMiddleware


@pytest.mark.asyncio
@pytest.mark.service
async def test_task_body_sees_current_org_id_via_middleware(db_session) -> None:  # type: ignore[no-untyped-def]
    """Task body receives the org_id from enqueue metadata via the middleware.

    Flow:
    1. Register a task that captures current_org_id() into a shared list.
    2. Enqueue it with explicit metadata={"org_id": str(expected_org)}.
    3. Drain into an InMemoryBroker (await_inplace=True) wired with the middleware.
    4. Assert the captured org_id matches.
    """
    expected_org: UUID = uuid4()
    captured: list[UUID | None] = []

    # Register a temporary task body that reads the contextvar.
    async def _marker_task() -> None:
        captured.append(current_org_id())

    ref = task("middleware_test_task")(_marker_task)
    with scoped_task_registration(ref):
        # Build an isolated in-memory broker with the middleware wired in.
        broker = InMemoryBroker(await_inplace=True)
        broker.add_middlewares(OrgContextMiddleware())
        # Register the same task body on this broker so the dispatcher can find it.
        broker.task(task_name=ref.name)(lambda: None)
        # Replace the registered function with our real async body so it executes.
        broker.local_task_registry[ref.name].original_func = _marker_task

        await broker.startup()
        try:
            # Enqueue to the outbox with explicit org metadata.
            await enqueue(
                ref,
                args={},
                metadata={"org_id": str(expected_org)},
                session=db_session,
            )
            await db_session.commit()

            # Drain: the dispatcher calls kicker.kiq() on our in-memory broker.
            dispatcher = await _taskiq_dispatcher_for(broker)
            await drain_once(db_session, dispatcher=dispatcher)
            await db_session.commit()
        finally:
            await broker.shutdown()

    assert len(captured) == 1, "task body did not run"
    assert captured[0] == expected_org, (
        f"expected org_id={expected_org!r} inside task body, got {captured[0]!r}"
    )
