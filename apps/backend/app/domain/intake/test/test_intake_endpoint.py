"""POST /api/intake/{type} — happy path, idempotent duplicate, rejection codes."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core.events import (
    Event,
    EventFilter,
    subscribe,
)
from app.core.tasks import drain_once
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowExecutionRow,
    WorkflowState,
    scoped_engine,
)
from app.domain.intake import (
    IntakePrepared,
    IntakeRejectedError,
    register_intake_type,
)
from app.domain.intake import web as _intake_web  # noqa: F401 — registers routes
from app.domain.intake.registry import _reset_registry_for_tests
from app.domain.tickets import TicketRow


class _StubIntakeType:
    """Intake type used in tests — no GitHub. Header `x-stub-auth: ok` is
    required (a missing/wrong header maps to `bad_signature` → 401)."""

    name = "stub_pr"

    def __init__(self, org_id: UUID) -> None:
        self._org_id = org_id

    async def handle(self, *, headers, body, session) -> IntakePrepared:
        if headers.get("x-stub-auth") != "ok":
            raise IntakeRejectedError("bad_signature")
        idempotency_key = headers.get("x-stub-idempotency", "default-key")
        return IntakePrepared(
            org_id=self._org_id,
            workflow_name="stub_pr_v1",
            idempotency_key=idempotency_key,
            title="stub-pr",
            description="",
            source_external_id="stub:1",
            repo_external_id="me/repo",
            payload={"hello": "world"},
        )


class _NoopLocal:
    kind = "Noop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


@pytest_asyncio.fixture
async def stub_intake(db_session):  # type: ignore[no-untyped-def]
    """Spin up an isolated engine + the stub intake type. The workflow has
    one Local step that completes immediately, so the workflow path is
    exercised end-to-end without needing a Workspace dispatcher."""
    _reset_registry_for_tests()

    with scoped_engine() as eng:
        eng.register_command(_NoopLocal())
        eng.register_workflow(
            Workflow(
                name="stub_pr_v1",
                version=1,
                steps=(
                    Step(
                        id="only",
                        command_kind="Noop",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="only",
            )
        )

        org_id = uuid4()
        register_intake_type(_StubIntakeType(org_id))

        yield {"org_id": org_id}

    # Restore real intake registry (re-import re-registers github_pr).
    _reset_registry_for_tests()
    import importlib  # noqa: PLC0415

    import app.domain.intake as intake_mod  # noqa: PLC0415

    importlib.reload(intake_mod)


def _app() -> FastAPI:

    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"intake"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_unknown_intake_type_404(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post("/api/intake/ghost", content=b"{}")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_intake_type"


@pytest.mark.asyncio
async def test_bad_signature_returns_401(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post("/api/intake/stub_pr", content=b"{}", headers={})
    assert r.status_code == 401
    assert r.json()["error"] == "bad_signature"


@pytest.mark.asyncio
async def test_happy_path_creates_ticket_and_workflow(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/intake/stub_pr",
            content=b"{}",
            headers={"x-stub-auth": "ok", "x-stub-idempotency": "key-1"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created"
    ticket_id = UUID(body["ticket_id"])
    workflow_execution_id = UUID(body["workflow_execution_id"])

    # Ticket row created with the type + idempotency_key + workflow link.
    ticket = await db_session.get(TicketRow, ticket_id)
    assert ticket is not None
    assert ticket.type == "stub_pr"
    assert ticket.idempotency_key == "key-1"
    assert ticket.payload == {"hello": "world"}
    assert ticket.current_workflow_execution_id == workflow_execution_id
    assert ticket.org_id == stub_intake["org_id"]

    # Workflow execution row created.
    wfx = await db_session.get(WorkflowExecutionRow, workflow_execution_id)
    assert wfx is not None
    assert wfx.workflow_name == "stub_pr_v1"
    assert str(wfx.ticket_id) == str(ticket_id)

    # Draining the enqueued route_workflow task drives the single-step
    # workflow to DONE — proves the task was enqueued correctly at intake.
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(10):
        n = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if n == 0:
            break

    await db_session.refresh(wfx)
    assert wfx.state == WorkflowState.DONE.value


@pytest.mark.asyncio
async def test_happy_path_publishes_ticket_status_changed_event(db_session, stub_intake) -> None:
    """Intake-created tickets must broadcast TicketStatusChanged so the SSE
    subscriber invalidates the list query — otherwise the row is invisible
    in the UI until something else nudges the cache."""
    seen: list[Event] = []

    async def consume() -> None:
        async for ev in subscribe(EventFilter(kinds=["ticket_status_changed"])):
            seen.append(ev)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    async with _client() as c:
        r = await c.post(
            "/api/intake/stub_pr",
            content=b"{}",
            headers={"x-stub-auth": "ok", "x-stub-idempotency": "event-key"},
        )
    assert r.status_code == 200, r.text

    await asyncio.wait_for(consumer, timeout=1.0)
    assert len(seen) == 1
    evt = seen[0]
    assert evt.kind == "ticket_status_changed"
    assert evt.new_status == "pending"  # type: ignore[attr-defined]
    assert evt.previous_status is None  # type: ignore[attr-defined]
    assert str(evt.ticket_id) == r.json()["ticket_id"]


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_returns_existing_ticket(db_session, stub_intake) -> None:
    headers = {"x-stub-auth": "ok", "x-stub-idempotency": "dup-key"}
    async with _client() as c:
        first = await c.post("/api/intake/stub_pr", content=b"{}", headers=headers)
        second = await c.post("/api/intake/stub_pr", content=b"{}", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "created"
    assert second.json()["status"] == "duplicate"
    assert first.json()["ticket_id"] == second.json()["ticket_id"]

    # Only one workflow execution exists for the (single) ticket.
    rows = (
        (
            await db_session.execute(
                select(WorkflowExecutionRow).where(
                    WorkflowExecutionRow.ticket_id == UUID(first.json()["ticket_id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
