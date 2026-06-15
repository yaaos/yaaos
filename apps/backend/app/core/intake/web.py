"""HTTP wiring for `core/intake` — `POST /api/intake/{type}` is the only
entry point for external signals.

For each registered `IntakeType`, the endpoint:

1. Reads body + headers.
2. Hands them to `type.handle(...)` for verification + parsing.
3. The handler returns `IntakeSideEffect` — it already applied its mutations
   against the endpoint's session; just commit and return 200.
4. All in a single transaction; the outbox drain delivers any task enqueued
   inside that transaction after commit.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse

from app.core.auth import public_route
from app.core.database import session as db_session
from app.core.intake.registry import (
    IntakeRejectedError,
    IntakeSideEffect,
    IntakeType,
    get_intake_type,
)
from app.core.webserver import RouteSpec, register_routes

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
            outcome = await handler.handle(headers=headers, body=body, session=s)
        except IntakeRejectedError as exc:
            log.info(
                "intake.rejected",
                type=type,
                kind=exc.kind,
                message=str(exc),
            )
            code = _REJECTION_STATUS.get(exc.kind, 400)
            return JSONResponse(status_code=code, content={"error": exc.kind, "detail": str(exc)})

        # All handlers return IntakeSideEffect — they manage their own ticket
        # creation atomically inside the endpoint's session.
        assert isinstance(outcome, IntakeSideEffect)
        await s.commit()
        return JSONResponse(status_code=200, content={"status": "side_effect", "detail": outcome.detail})


register_routes(RouteSpec(module_name="intake", router=router, url_prefix="/api/intake"))
