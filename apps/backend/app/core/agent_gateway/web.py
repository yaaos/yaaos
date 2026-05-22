"""HTTP routes for the WorkspaceAgent wire protocol.

Five endpoints mounted under `/v1/`. The implementation calls into
`core.agent_gateway.service`; this module is the FastAPI shim.

`/v1/identity/exchange` runs the real Vault-AWS-auth pattern via
`core.agent_gateway.sts_verifier`: the agent's sigv4-signed STS
GetCallerIdentity is replayed against AWS, the returned ARN is matched
against `orgs.registered_iam_arn`, and a `workspace_agents` row is
persisted before a 24-hour bearer is issued. The remaining endpoints
still use a placeholder bearer verifier (any non-empty token) pending
the bearer-issuance ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.core.agent_gateway.service import (
    claim_next,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.agent_gateway.subscribers import get_registry as _get_subscriber_registry
from app.core.agent_gateway.types import (
    AgentEvent,
    ClaimRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    IdentityExchangeRequest,
    IdentityExchangeResponse,
    StaleClaimError,
    UnauthorizedError,
    WorkspaceEvent,
)
from app.core.auth.context import public_route
from app.core.database import session as db_session
from app.core.sse_pubsub import channel_for
from app.core.sse_pubsub import publish as sse_publish
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("agent_gateway.web")

router = APIRouter()


# ── Placeholder bearer verifier ─────────────────────────────────────────


def _verify_bearer(authorization: str | None) -> None:
    """Phase 5 placeholder. Phase 7 wires the real verifier that validates
    the bearer against the issuance log + checks expiry."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("empty bearer")


def _bearer_dep(authorization: str | None = Header(default=None)) -> None:
    try:
        _verify_bearer(authorization)
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "detail": str(exc)}) from exc


# ── Endpoints ───────────────────────────────────────────────────────────


@router.post("/identity/exchange", dependencies=[Depends(public_route)])
async def exchange_identity(request: IdentityExchangeRequest) -> IdentityExchangeResponse:
    """Vault AWS-auth pattern: agent supplies a sigv4-signed STS
    GetCallerIdentity request; control plane replays it against AWS;
    extracted ARN must match an `orgs.registered_iam_arn` row. On
    success persists/updates a `workspace_agents` row + returns a
    24-hour bearer scoped to the resolved agent_id.

    Failure modes:
    - empty `signed_request` → 401 `unauthorized`
    - parse / shape / non-STS-endpoint / wrong-body / replay-rejected
      → 401 `unauthorized` with `sts_verification_failed` detail
    - ARN doesn't match any registered org → 403 `forbidden_unregistered_arn`
    """
    if not request.signed_request:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "empty signed_request"},
        )

    from app.core.agent_gateway.service import ensure_agent_row  # noqa: PLC0415
    from app.core.agent_gateway.sts_verifier import (  # noqa: PLC0415
        InvalidSignedRequestError,
        verify_identity,
    )

    try:
        arn = await verify_identity(request.signed_request)
    except InvalidSignedRequestError as exc:
        log.info("identity_exchange.verify_failed", error=str(exc))
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "sts_verification_failed"},
        )

    # Match the verified ARN against `orgs.registered_iam_arn`. The
    # agent_gateway is core — we touch the orgs table directly rather
    # than importing the domain (same pattern as the workspace_agents
    # writes elsewhere in this module).
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    async with db_session() as s:
        org_row = (
            await s.execute(
                sa_text("SELECT id FROM orgs WHERE registered_iam_arn = :arn LIMIT 1"),
                {"arn": arn},
            )
        ).first()
        if org_row is None:
            log.info("identity_exchange.arn_not_registered", arn=arn)
            raise HTTPException(
                status_code=403,
                detail={"error": "forbidden", "detail": "forbidden_unregistered_arn"},
            )
        org_id = org_row[0]

        agent_id = await ensure_agent_row(
            org_id=org_id,
            agent_pod_id=request.agent_pod_id,
            iam_arn=arn,
            version=request.version or "0.0.1",
            session=s,
        )
        await s.commit()

    return IdentityExchangeResponse(
        bearer=f"placeholder-{uuid4()}",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        agent_id=agent_id,
    )


@router.post("/agents/{agent_id}/heartbeat", dependencies=[Depends(_bearer_dep)])
async def heartbeat(
    request: HeartbeatRequest,
    agent_id: UUID = Path(...),
) -> HeartbeatResponse:
    async with db_session() as s:
        response = await record_heartbeat(agent_id, request, session=s)
        await s.commit()
    return response


@router.post("/agents/{agent_id}/commands/claim", dependencies=[Depends(_bearer_dep)])
async def claim_command(
    request: ClaimRequest,
    agent_id: UUID = Path(...),
) -> Response:
    cmd = await claim_next(agent_id, wait_seconds=request.wait_seconds)
    if cmd is None:
        return Response(status_code=204)
    return JSONResponse(status_code=200, content=cmd.model_dump(mode="json"))


@router.post("/workspaces/{workspace_id}/events", dependencies=[Depends(_bearer_dep)])
async def post_workspace_event(
    event: WorkspaceEvent,
    workspace_id: UUID = Path(...),
) -> Response:
    if event.workspace_id != workspace_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_request", "detail": "path and body workspace_id disagree"},
        )
    try:
        async with db_session() as s:
            await record_workspace_event(event, session=s)
            await s.commit()
    except StaleClaimError as exc:
        log.info("agent.workspace_event.stale", workspace_id=str(workspace_id), error=str(exc))
        return JSONResponse(status_code=410, content={"error": "stale_claim", "detail": str(exc)})
    return Response(status_code=200)


@router.post("/commands/{command_id}/events", dependencies=[Depends(_bearer_dep)])
async def post_command_event(
    event: AgentEvent,
    command_id: UUID = Path(...),
) -> Response:
    if event.command_id != command_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_request", "detail": "path and body command_id disagree"},
        )
    try:
        async with db_session() as s:
            await record_agent_event(event, session=s)
            await s.commit()
    except StaleClaimError as exc:
        log.info("agent.command_event.stale", command_id=str(command_id), error=str(exc))
        return JSONResponse(status_code=410, content={"error": "stale_claim", "detail": str(exc)})
    return Response(status_code=200)


# ── Activity WebSocket (Phase 8b) ───────────────────────────────────────


@router.websocket("/agents/{agent_id}/activity")
async def activity_ws(websocket: WebSocket, agent_id: UUID = Path(...)) -> None:
    """Bidirectional activity-stream channel.

    Auth on upgrade: the supervisor includes `Authorization: Bearer <token>`
    in the WebSocket handshake. Phase 8b foundations uses the same
    placeholder verifier as the HTTPS endpoints — any non-empty bearer
    passes. Phase 7 follow-on swaps in the real STS-issued bearer check.

    Protocol:
      - **WorkspaceAgent → backend:** `{"type": "activity_batch", "workspace_id": "...", "events": [...]}`.
        Backend publishes each event to `activity:{workflow_execution_id}`
        via `core/sse_pubsub`. The SSE handler in `web.py` (Phase 8b
        follow-on) consumes them per workflow execution.
      - **Backend → WorkspaceAgent:** `{"type": "subscribe", "workspace_id": "..."}` /
        `{"type": "unsubscribe", "workspace_id": "..."}`. Driven by the
        subscriber registry's 0→1 / 1→0 transitions.

    Failure modes:
      - Missing/empty `Authorization` header → close with 4401.
      - Disconnect at any time → registry unregisters the sender; SSE
        subscribers that arrive later won't reach this agent until it
        reconnects (the reconnect handler in the follow-on re-derives
        and re-sends `subscribe` for any still-active subscribers).
    """
    auth = websocket.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth.split(" ", 1)[1].strip():
        await websocket.close(code=4401)
        return
    await websocket.accept()

    registry = _get_subscriber_registry()

    async def _send(message: dict) -> None:
        await websocket.send_text(json.dumps(message))

    await registry.register_sender(agent_id, _send)
    log.info("agent_gateway.ws.connected", agent_id=str(agent_id))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("agent_gateway.ws.bad_json", agent_id=str(agent_id))
                continue
            kind = msg.get("type")
            if kind == "activity_batch":
                workflow_execution_id = msg.get("workflow_execution_id")
                events = msg.get("events") or []
                if not workflow_execution_id or not isinstance(events, list):
                    log.warning(
                        "agent_gateway.ws.malformed_batch",
                        agent_id=str(agent_id),
                        keys=list(msg.keys()),
                    )
                    continue
                channel = channel_for(str(workflow_execution_id))
                for event in events:
                    if isinstance(event, dict):
                        await sse_publish(channel, event)
            else:
                log.info(
                    "agent_gateway.ws.unknown_kind",
                    agent_id=str(agent_id),
                    kind=kind,
                )
    except WebSocketDisconnect:
        pass
    finally:
        await registry.unregister_sender(agent_id)
        log.info("agent_gateway.ws.disconnected", agent_id=str(agent_id))


register_routes(RouteSpec(module_name="agent_gateway", router=router, url_prefix="/api/v1"))
