"""HTTP wiring for `domain/intake` — the generic `POST /api/intake/{type}`
endpoint (M05 Phase 2).

For each registered `IntakeType`, the endpoint:

1. Reads body + headers.
2. Hands them to `type.handle(...)` for verification + parsing.
3. Calls `domain/tickets.create(type, payload, idempotency_key)` —
   idempotent: a second request with the same key returns the same ticket
   id without creating a workflow execution.
4. On first creation, calls `core/workflow.start(type.workflow_name, ticket_id)`
   and stamps the resulting execution id on the ticket.
5. All in a single transaction; the outbox drain delivers the initial
   routing task after commit.

The legacy `POST /api/github/webhook` continues to handle the existing
GitHub flows; the new endpoint is the M05 surface that funnels work
through the workflow engine.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse

from app.core.auth.context import public_route
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.core.workflow import get_engine
from app.domain import tickets
from app.domain.intake.registry import (
    IntakeRejectedError,
    IntakeType,
    get_intake_type,
)

log = structlog.get_logger("intake.web")

router = APIRouter()


_REJECTION_STATUS = {
    "bad_signature": status.HTTP_401_UNAUTHORIZED,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "unsupported": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


@router.post("/{type}", dependencies=[Depends(public_route)])
async def post_intake(request: Request, type: str = Path(...)) -> JSONResponse:
    handler: IntakeType | None = get_intake_type(type)
    if handler is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_intake_type"})

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    async with db_session() as s:
        try:
            prepared = await handler.handle(headers=headers, body=body, session=s)
        except IntakeRejectedError as exc:
            log.info(
                "intake.rejected",
                type=type,
                kind=exc.kind,
                message=str(exc),
            )
            code = _REJECTION_STATUS.get(exc.kind, 400)
            return JSONResponse(status_code=code, content={"error": exc.kind, "detail": str(exc)})

        ticket_id, created = await tickets.create(
            type=type,
            payload=dict(prepared.payload),
            idempotency_key=prepared.idempotency_key,
            org_id=prepared.org_id,
            title=prepared.title,
            description=prepared.description,
            source=type,
            source_external_id=prepared.source_external_id,
            repo_external_id=prepared.repo_external_id,
            session=s,
        )

        if not created:
            await s.commit()
            return JSONResponse(
                status_code=200,
                content={"status": "duplicate", "ticket_id": str(ticket_id)},
            )

        engine = get_engine()
        workflow_execution_id = await engine.start(
            workflow_name=handler.workflow_name,
            ticket_id=str(ticket_id),
            session=s,
        )
        from uuid import UUID  # noqa: PLC0415

        await tickets.attach_workflow_execution(ticket_id, UUID(workflow_execution_id), session=s)
        await s.commit()

    return JSONResponse(
        status_code=200,
        content={
            "status": "created",
            "ticket_id": str(ticket_id),
            "workflow_execution_id": workflow_execution_id,
        },
    )


register_routes(RouteSpec(module_name="intake", router=router, url_prefix="/api/intake"))
