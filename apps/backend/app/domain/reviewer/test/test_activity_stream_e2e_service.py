"""End-to-end activity-stream test for the in-memory provider path.

Drives `pr_review_v1` through the workflow engine with a Fake
coding-agent that emits a canned `ActivityEvent` sequence. Asserts the
SPA's SSE consumer (subscribing via `core/sse_pubsub.subscribe`) sees
each event verbatim — proving the in-memory taskiq worker path
publishes activity straight to `sse_pubsub` without needing the
remote-agent WebSocket transport.

Closes the activity-stream-against-both-providers audit row for
the in_memory side; the remote-agent side is covered by
`core/agent_gateway/test/` activity WebSocket tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.plugin_kit import PluginMeta
from app.core.sse_pubsub import (
    channel_for,
    subscribe,
)
from app.core.sse_pubsub.service import _reset_for_tests as _reset_pubsub
from app.core.tasks.drain import drain_once
from app.core.tasks.models import OutboxEntryRow
from app.core.workflow import WorkflowState, get_engine
from app.core.workflow.models import WorkflowExecutionRow
from app.core.workflow.service import _reset_for_tests
from app.core.workspace import (
    WorkspaceTicketContext,
    clear_workflow_context_provider,
    clear_workspace_providers,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.domain.coding_agent.types import ActivityEvent
from app.domain.reviewer.commands import ALL_LOCAL_COMMANDS, ALL_WORKSPACE_COMMANDS
from app.domain.reviewer.workflows import pr_review_v1
from app.domain.tickets import create as create_ticket
from app.testing.fake_coding_agent import register_fake_coding_agent

pytestmark = pytest.mark.usefixtures("redis_or_skip")


class _StubWorkspaceProvider:
    meta = PluginMeta(id="in_process", type="workspace", display_name="stub")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None


class _StaticCtxProvider:
    def __init__(self, ctx: WorkspaceTicketContext) -> None:
        self._ctx = ctx

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._ctx


@pytest.fixture
def _engine_with_in_memory():
    _reset_for_tests()
    clear_workspace_providers()
    clear_workflow_context_provider()
    _reset_pubsub()
    register_workspace_provider(_StubWorkspaceProvider())
    eng = get_engine()
    from app.core.workspace.commands import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415

    for cmd in (*ALL_LIFECYCLE_COMMANDS, *ALL_WORKSPACE_COMMANDS, *ALL_LOCAL_COMMANDS):
        eng.register_command(cmd)
    eng.register_workflow(pr_review_v1)
    yield eng
    _reset_for_tests()
    clear_workspace_providers()
    clear_workflow_context_provider()
    _reset_pubsub()


async def _drain(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.core.tasks.broker import get_broker  # noqa: PLC0415

    for _ in range(50):
        rows = (
            (
                await db_session.execute(
                    select(OutboxEntryRow)
                    .where(OutboxEntryRow.dispatched_at.is_(None))
                    .order_by(OutboxEntryRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return

        async def _dispatcher(kind: str, payload: dict) -> None:
            assert kind == "taskiq_enqueue"
            decorated = get_broker().find_task(payload["task_name"])
            assert decorated is not None
            await decorated.original_func(**payload["args"])

        await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()


async def test_in_memory_review_publishes_activity_to_sse_pubsub(  # type: ignore[no-untyped-def]
    db_session, _engine_with_in_memory
):
    """Subscribe to the workflow's activity channel; run `pr_review_v1`
    with a Fake coding-agent that emits a 3-event sequence; assert the
    SSE channel saw exactly those 3 events in order."""
    org_id = uuid4()
    ticket_id, _ = await create_ticket(
        type="github_pr",
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": [],
            "author_login": "alice",
            "pr_external_id": "42",
            "head_sha": "deadbeefcafe",
            "base_sha": "babecafe",
        },
        idempotency_key=f"act-{uuid4()}",
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id="42",
        plugin_id="github",
        repo_external_id="me/repo",
        session=db_session,
    )
    register_workflow_context_provider(
        _StaticCtxProvider(
            WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "deadbeefcafe", "base_sha": "babecafe"},
            )
        )
    )

    canned_events = [
        ActivityEvent(
            ts=datetime.now(UTC),
            kind="session_start",
            message="Session started · model opus",
            detail={"model": "opus"},
        ),
        ActivityEvent(
            ts=datetime.now(UTC),
            kind="tool_call_started",
            message="Read: src/x.py",
            detail={"tool": "Read"},
        ),
        ActivityEvent(
            ts=datetime.now(UTC),
            kind="result",
            message="Review complete",
            detail={},
        ),
    ]

    with register_fake_coding_agent() as fake:
        fake.activity_events = canned_events

        wfx_id = await _engine_with_in_memory.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            workspace_provider="in_memory",
            session=db_session,
        )
        await db_session.commit()

        received: list[dict] = []

        async def _reader() -> None:
            async for event in subscribe(channel_for(wfx_id)):
                received.append(event)
                if len(received) >= 3:
                    return

        reader_task = asyncio.create_task(_reader())
        # Give the subscriber a tick to register before we drain.
        await asyncio.sleep(0.01)

        await _drain(db_session)

        # Pull the reader to completion (with a generous timeout for the
        # in-process pubsub).
        await asyncio.wait_for(reader_task, timeout=2.0)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx.state == WorkflowState.DONE.value

    assert len(received) == 3
    assert [e["kind"] for e in received] == ["session_start", "tool_call_started", "result"]
    assert received[0]["detail"]["model"] == "opus"
    assert received[1]["detail"]["tool"] == "Read"
