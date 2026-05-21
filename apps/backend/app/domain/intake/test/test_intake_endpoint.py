"""POST /api/intake/{type} — happy path, idempotent duplicate, rejection codes."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core.outbox.models import OutboxEntryRow
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowEngine,
    WorkflowExecutionRow,
    _reset_for_tests,
)
from app.domain.intake import (
    IntakePrepared,
    IntakeRejectedError,
    _reset_registry_for_tests,
    register_intake_type,
)
from app.domain.intake import web as _intake_web  # noqa: F401 — registers routes
from app.domain.tickets.models import TicketRow


class _StubIntakeType:
    """Intake type used in tests — no GitHub. Header `x-stub-auth: ok` is
    required (a missing/wrong header maps to `bad_signature` → 401)."""

    name = "stub_pr"
    workflow_name = "stub_pr_v1"

    def __init__(self, org_id: UUID) -> None:
        self._org_id = org_id

    async def handle(self, *, headers, body, session) -> IntakePrepared:
        if headers.get("x-stub-auth") != "ok":
            raise IntakeRejectedError("bad_signature")
        idempotency_key = headers.get("x-stub-idempotency", "default-key")
        return IntakePrepared(
            org_id=self._org_id,
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
async def stub_intake(db_session):
    """Spin up an isolated engine + the stub intake type. The workflow has
    one Local step that completes immediately, so the workflow path is
    exercised end-to-end without needing a Workspace dispatcher."""
    _reset_for_tests()
    _reset_registry_for_tests()

    eng = WorkflowEngine()
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
    import app.core.workflow.service as svc  # noqa: PLC0415

    svc._engine = eng

    org_id = uuid4()
    register_intake_type(_StubIntakeType(org_id))

    yield {"org_id": org_id}

    # Restore real intake registry (re-import re-registers github_pr).
    _reset_registry_for_tests()
    import importlib  # noqa: PLC0415

    import app.domain.intake as intake_mod  # noqa: PLC0415

    importlib.reload(intake_mod)
    _reset_for_tests()


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    spec = _specs["intake"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/intake")
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

    # Initial route_workflow task is in the outbox awaiting drain.
    outbox_rows = (
        (await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.kind == "taskiq_enqueue")))
        .scalars()
        .all()
    )
    assert any(
        row.payload.get("task_name") == "workflow.route_workflow"
        and row.payload.get("args", {}).get("workflow_execution_id") == str(workflow_execution_id)
        for row in outbox_rows
    )


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
