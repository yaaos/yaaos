"""HTTP routes for the WorkspaceAgent wire protocol.

Five endpoints mounted under `/v1/` + one WebSocket. The implementation
calls into `core.agent_gateway.service`; this module is the FastAPI shim.

`/v1/identity/exchange` runs the Vault-AWS-auth pattern via
`core.agent_gateway.sts_verifier`: the agent's sigv4-signed STS
GetCallerIdentity is replayed against AWS, the returned ARN is
canonicalized (assumed-role → role) and matched against
`orgs.registered_iam_arn`, the URL region is checked against
`orgs.aws_region`, a `workspace_agents` row is persisted, and a 24-hour
bearer is issued via `core.agent_gateway.bearers`. Every other gateway
endpoint and the WebSocket upgrade authenticate by looking that bearer
up in the ledger.
"""

from __future__ import annotations

import json
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.core.agent_gateway import bearers
from app.core.agent_gateway.rate_limit import RateLimitedError, check_identity_exchange
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
from app.core.audit_log import ActorKind
from app.core.auth import org_context, public_route, require_org_context
from app.core.database import session as db_session
from app.core.sse import publish_workspace_activity
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("agent_gateway.web")

router = APIRouter()


# ── Bearer verifier (real ledger lookup) ────────────────────────────────


async def _verify_bearer(authorization: str | None) -> bearers.BearerContext:
    """Hash the incoming bearer + look it up in `bearer_tokens`. Returns
    the resolved `BearerContext`; raises `UnauthorizedError` on missing,
    malformed, expired, or revoked tokens. Opaque rejection — no detail
    distinguishing "expired" from "revoked" from "never existed"."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise UnauthorizedError("empty bearer")
    ctx = await bearers.verify(token)
    if ctx is None:
        raise UnauthorizedError("invalid bearer")
    return ctx


async def _bearer_dep(authorization: str | None = Header(default=None)) -> bearers.BearerContext:
    try:
        return await _verify_bearer(authorization)
    except UnauthorizedError as exc:
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "detail": str(exc)}) from exc


# ── Endpoints ───────────────────────────────────────────────────────────


@router.post("/identity/exchange", dependencies=[Depends(public_route)])
async def exchange_identity(
    request: IdentityExchangeRequest, http_request: Request
) -> IdentityExchangeResponse:
    """Vault AWS-auth pattern: agent supplies a sigv4-signed STS
    GetCallerIdentity request; control plane replays it against AWS,
    canonicalizes the returned ARN, matches against
    `orgs.registered_iam_arn`, checks the signed URL's region against
    `orgs.aws_region`, persists/updates a `workspace_agents` row, and
    issues a 24-hour bearer via `core.agent_gateway.bearers`.

    Failure modes:
    - empty `signed_request` → 401 `unauthorized`, no audit row
    - verifier failure (parse / shape / endpoint / body / replay /
      aws-rejected / clock-skew) → 401, structlog warning categorized
      by `FailureCategory`
    - canonical ARN doesn't match any registered org → 403
      `forbidden_unregistered_arn`
    - signed URL's region != `orgs.aws_region` → 401
      `sts_verification_failed` with category `region_mismatch`
    """
    if not request.signed_request:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "empty signed_request"},
        )

    source_ip = http_request.client.host if http_request.client is not None else None

    try:
        await check_identity_exchange(source_ip=source_ip, agent_pod_id=str(request.agent_pod_id))
    except RateLimitedError as exc:
        log.warning(
            "identity_exchange.rate_limited",
            axis=exc.axis,
            limit=exc.limit,
            source_ip=source_ip,
        )
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "detail": exc.axis},
        )

    from app.core.agent_gateway.service import ensure_agent_row  # noqa: PLC0415
    from app.core.agent_gateway.sts_verifier import (  # noqa: PLC0415
        InvalidSignedRequestError,
        verify_identity,
    )

    try:
        verified = await verify_identity(request.signed_request)
    except InvalidSignedRequestError as exc:
        log.warning(
            "identity_exchange.verify_failed",
            category=exc.category.value,
            source_ip=source_ip,
        )
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "sts_verification_failed"},
        )

    from sqlalchemy import text as sa_text  # noqa: PLC0415

    async with db_session() as s:
        org_row = (
            await s.execute(
                sa_text("SELECT id, aws_region FROM orgs WHERE registered_iam_arn = :arn LIMIT 1"),
                {"arn": verified.canonical_arn},
            )
        ).first()
        if org_row is None:
            log.warning(
                "identity_exchange.arn_not_registered",
                canonical_arn=verified.canonical_arn,
                source_ip=source_ip,
            )
            raise HTTPException(
                status_code=403,
                detail={"error": "forbidden", "detail": "forbidden_unregistered_arn"},
            )
        org_id, org_region = org_row[0], org_row[1]

        # Region pinning: the signed request must target the same AWS
        # region the org has on file. Defeats cross-region replay of a
        # signature stolen from a different STS endpoint.
        if org_region and verified.region != org_region:
            log.warning(
                "identity_exchange.region_mismatch",
                org_region=org_region,
                attempted_region=verified.region,
                source_ip=source_ip,
            )
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthorized", "detail": "sts_verification_failed"},
            )

        agent_id = await ensure_agent_row(
            org_id=org_id,
            agent_pod_id=request.agent_pod_id,
            iam_arn=verified.canonical_arn,
            version=request.version or "0.0.1",
            session=s,
        )
        plaintext, record = await bearers.issue(
            agent_id=agent_id, org_id=org_id, session=s, source_ip=source_ip
        )
        await s.commit()

    log.info(
        "identity_exchange.success",
        agent_id=str(agent_id),
        org_id=str(org_id),
        bearer_id=str(record.id),
    )
    return IdentityExchangeResponse(
        bearer=plaintext,
        expires_at=record.expires_at,
        agent_id=agent_id,
    )


@router.post("/agents/{agent_id}/heartbeat")
async def heartbeat(
    request: HeartbeatRequest,
    agent_id: UUID = Path(...),
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> HeartbeatResponse:
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        async with db_session() as s:
            response = await record_heartbeat(agent_id, request, session=s)
            await s.commit()
    return response


@router.post("/agents/{agent_id}/commands/claim")
async def claim_command(
    request: ClaimRequest,
    agent_id: UUID = Path(...),
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> Response:
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        cmd = await claim_next(agent_id, wait_seconds=request.wait_seconds)
        if cmd is None:
            return Response(status_code=204)
        return JSONResponse(status_code=200, content=cmd.model_dump(mode="json"))


@router.post("/workspaces/{workspace_id}/events")
async def post_workspace_event(
    event: WorkspaceEvent,
    workspace_id: UUID = Path(...),
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> Response:
    if event.workspace_id != workspace_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_request", "detail": "path and body workspace_id disagree"},
        )
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        try:
            async with db_session() as s:
                await record_workspace_event(event, session=s)
                await s.commit()
        except StaleClaimError as exc:
            log.info("agent.workspace_event.stale", workspace_id=str(workspace_id), error=str(exc))
            return JSONResponse(status_code=410, content={"error": "stale_claim", "detail": str(exc)})
    return Response(status_code=200)


@router.post("/commands/{command_id}/events")
async def post_command_event(
    event: AgentEvent,
    command_id: UUID = Path(...),
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> Response:
    if event.command_id != command_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_request", "detail": "path and body command_id disagree"},
        )
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        try:
            async with db_session() as s:
                await record_agent_event(event, session=s)
                await s.commit()
        except StaleClaimError as exc:
            log.info("agent.command_event.stale", command_id=str(command_id), error=str(exc))
            return JSONResponse(status_code=410, content={"error": "stale_claim", "detail": str(exc)})
    return Response(status_code=200)


# ── Activity WebSocket ──────────────────────────────────────────────────


@router.websocket("/agents/{agent_id}/activity")
async def activity_ws(websocket: WebSocket, agent_id: UUID = Path(...)) -> None:
    """Bidirectional activity-stream channel.

    Auth on upgrade: the supervisor includes `Authorization: Bearer <token>`
    in the WebSocket handshake, validated against `bearer_tokens` via
    `bearers.verify`.

    Protocol:
      - **WorkspaceAgent → backend:** `{"type": "activity_batch", "workflow_execution_id": "...", "events": [...]}`.
        Backend publishes each event to the org-scoped channel via
        `publish_workspace_activity(org_id, workflow_execution_id, payload)`.
      - **Backend → WorkspaceAgent:** `{"type": "subscribe", "workspace_id": "...", "workflow_execution_id": "..."}` /
        `{"type": "unsubscribe", "workspace_id": "...", "workflow_execution_id": "..."}`.
        Driven by the subscriber registry's 0→1 / 1→0 transitions.
        The agent caches the mapping so its `activity_batch` outbound
        carries the right `workflow_execution_id` keyed by the
        `workspace_id` it learned at subscribe time.

    Failure modes:
      - Missing/malformed/expired/revoked bearer → close with 4401 on
        upgrade, never accept the WebSocket. Opaque rejection (no oracle
        distinguishing expired from revoked from never-seen).
      - Bearer is valid but `agent_id` in the path doesn't match the
        bearer's resolved agent → 4403. Stops a stolen bearer from one
        pod being used to impersonate another pod's WebSocket.
      - Disconnect at any time → registry unregisters the sender; SSE
        subscribers that arrive later won't reach this agent until it
        reconnects.
    """
    auth = websocket.headers.get("authorization", "")
    try:
        ctx = await _verify_bearer(auth)
    except UnauthorizedError:
        await websocket.close(code=4401)
        return
    if ctx.agent_id != agent_id:
        log.warning(
            "agent_gateway.ws.agent_mismatch",
            bearer_agent_id=str(ctx.agent_id),
            path_agent_id=str(agent_id),
        )
        await websocket.close(code=4403)
        return
    await websocket.accept()

    registry = _get_subscriber_registry()

    async def _send(message: dict) -> None:
        await websocket.send_text(json.dumps(message))

    async with org_context(ctx.org_id, ActorKind.WORKSPACE, actor_id=ctx.agent_id):
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
                    for event in events:
                        if isinstance(event, dict):
                            await publish_workspace_activity(
                                org_id=require_org_context(),
                                workflow_execution_id=UUID(workflow_execution_id),
                                payload=event,
                            )
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
