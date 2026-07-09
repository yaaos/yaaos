"""Service test: `publish_general_after_commit`'s post-commit SSE publish
emits a `spawn:sse.publish_general` span sharing the calling span's trace_id.

Regression guard for routing the after-commit SSE publish through `spawn()`
instead of raw `asyncio.create_task`. The change makes the publish visible
in the calling request's trace.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from opentelemetry import trace

from app.core.observability import active_task_count
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


@pytest.mark.asyncio
@pytest.mark.usefixtures("redis_or_skip")
async def test_sse_publish_emits_spawn_span_in_calling_trace(db_session) -> None:
    """A commit that carries a stashed `publish_general_after_commit` event
    triggers an SSE publish routed through `spawn()`. The resulting
    `spawn:sse.publish_general` span must share the trace_id of the outer
    span active when the commit happened."""
    tracer = trace.get_tracer("test.sse.spawn")
    with span_capture() as exporter:
        with tracer.start_as_current_span("outer-request") as outer_span:
            upstream_trace_id = outer_span.get_span_context().trace_id

            publish_general_after_commit(
                db_session,
                org_id=uuid.uuid4(),
                kind=GeneralEventKind.TICKET_STATUS_CHANGED,
                payload={"ticket_id": str(uuid.uuid4())},
            )
            await db_session.commit()

        # `spawn()` schedules the publish via `asyncio.create_task` on the
        # next event-loop tick after commit — poll until it (a real Redis
        # publish) actually finishes rather than assuming a fixed tick count.
        for _ in range(200):
            if active_task_count() == 0:
                break
            await asyncio.sleep(0.01)

    spans = exporter.get_finished_spans()
    spawn_spans = [s for s in spans if s.name == "spawn:sse.publish_general"]
    assert spawn_spans, (
        f"expected at least one 'spawn:sse.publish_general' span; got {[s.name for s in spans]}"
    )
    for sp in spawn_spans:
        assert sp.context.trace_id == upstream_trace_id, (
            f"spawn:sse.publish_general trace_id {sp.context.trace_id:032x} != "
            f"outer trace_id {upstream_trace_id:032x}; "
            "spawn() must propagate the calling context"
        )
