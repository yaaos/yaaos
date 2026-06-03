"""HTTP routes for the WorkspaceAgent wire protocol.

Five endpoints mounted under `/v1/` + one WebSocket. The implementation
calls into `core.agent_gateway.service`; this module is the FastAPI shim.

`POST /api/v1/agent/identity` runs the Vault-AWS-auth pattern via
`core.agent_gateway.sts_verifier`: the agent's sigv4-signed STS
GetCallerIdentity (in the `payload` field) is replayed against AWS, the
returned ARN is canonicalized (assumed-role → role) and matched against
`orgs.registered_iam_arn`, the URL region is checked against
`orgs.aws_region`, the `instance_id` (role-session-name) is derived from
the raw ARN, a `workspace_agents` row is found-or-created keyed on
`(org_id, instance_id)`, and a 1-hour bearer is issued via
`core.agent_gateway.bearers`. Every other gateway endpoint and the WebSocket
upgrade authenticate by looking that bearer up in the ledger.

The `X-Yaaos-Audience` header inside the sigv4 envelope must be present and
match the backend's `YAAOS_PUBLIC_HOSTNAME` setting; a missing or mismatched
audience causes a 401 `audience_mismatch`.

Per-endpoint authorization beyond bearer validity:
- All agent operational channels — `heartbeat`, `claim`, `command-events`,
  `workspace-events`, and the activity WebSocket — derive agent identity
  solely from the bearer. No `{agent_id}` path segment on these routes;
  `org_context` blocks cross-org access.
- `post_workspace_event` / `post_command_event` bind on `workspace_id` /
  `command_id`, which resolve to a workspace carrying an owning `agent_id`
  (`WorkspaceRow.agent_id`, set at create-dispatch). When the resolved
  workspace has an owner that isn't the bearer's agent → 403 `forbidden`
  (see `_require_workspace_owner`). This closes the within-org IDOR where
  one agent instance's bearer reports state for another agent instance's
  workspace. A command that resolves to no workspace (e.g. an agent-scoped
  `ConfigUpdate`, which has no `workspace_id`) or a workspace with a NULL
  `agent_id` (in-memory/legacy) carries no ownership edge to check:
  authorization falls back to the org scope (`org_context`) plus the
  stale-claim guard in the sink.
"""

from __future__ import annotations

import json
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.core.agent_gateway import bearers
from app.core.agent_gateway.org_arn_lookup import lookup_org_by_arn
from app.core.agent_gateway.rate_limit import RateLimitedError, check_identity_exchange
from app.core.agent_gateway.report_sink import get_report_sink
from app.core.agent_gateway.service import (
    claim_next,
    ensure_agent_row,
    mark_agent_shutdown,
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
from app.core.audit_log import Actor, ActorKind, audit
from app.core.auth import org_context, public_route, require_org_context
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.sse import GeneralEventKind, publish_general_after_commit, publish_workspace_activity
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("agent_gateway.web")

router = APIRouter()


class _IdentityExchangeFailedAudit(BaseModel):
    """Payload for `identity_exchange_failed` audit rows.

    Only written for org-attributable failures (region mismatch on a verified
    ARN that matched a registered org). No-org failures stay structlog-only —
    `audit_entries.org_id` is mandatory, so unresolvable ARNs can't be recorded.
    """

    category: str
    attempted_arn: str
    source_ip: str | None


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


def _require_workspace_owner(agent: bearers.BearerContext, owning_agent_id: UUID | None) -> None:
    """Reject when a workspace has an owning agent that isn't the bearer's.

    `owning_agent_id` is `WorkspaceRow.agent_id` resolved via the report sink.
    None means no ownership edge to enforce — the workspace has no owner
    (in-memory/legacy) or the command resolves to no workspace (e.g. an
    agent-scoped ConfigUpdate); authorization then falls back to org scope +
    the stale-claim guard.
    """
    if owning_agent_id is not None and owning_agent_id != agent.agent_id:
        log.warning(
            "agent_gateway.workspace_owner_mismatch",
            bearer_agent_id=str(agent.agent_id),
            owning_agent_id=str(owning_agent_id),
        )
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "detail": "bearer agent does not own this workspace"},
        )


# ── Endpoints ───────────────────────────────────────────────────────────


@router.post("/agent/identity", dependencies=[Depends(public_route)])
async def exchange_identity(
    request: IdentityExchangeRequest, http_request: Request
) -> IdentityExchangeResponse:
    """Vault AWS-auth pattern: agent supplies a sigv4-signed STS
    GetCallerIdentity in `payload`; control plane replays it against AWS,
    canonicalizes the returned ARN, matches against `orgs.registered_iam_arn`,
    checks the signed URL's region against `orgs.aws_region`, derives
    `instance_id` from the role-session-name, persists/updates a
    `workspace_agents` row keyed on `(org_id, instance_id)`, and issues a
    1-hour bearer via `core.agent_gateway.bearers`.

    The `X-Yaaos-Audience` header embedded in the sigv4 envelope must be
    present and match `YAAOS_PUBLIC_HOSTNAME`. This binds the signed
    request to a specific backend deployment and prevents replay against a
    different yaaos instance.

    Rotation: calling this endpoint again with a valid payload issues a new
    bearer without revoking the old one. The old bearer remains valid until
    its own `expires_at`. The agent atomically swaps the bearer in its HTTP
    client after the rotation response arrives.

    Failure modes:
    - empty `payload` → 401 `unauthorized`, no audit row
    - unsupported `kind` → 401 `unauthorized`
    - audience mismatch → 401 `audience_mismatch`
    - verifier failure (parse / shape / endpoint / body / replay /
      aws-rejected / clock-skew) → 401, structlog warning categorized
      by `FailureCategory`
    - canonical ARN doesn't match any registered org → 403
      `forbidden_unregistered_arn`
    - signed URL's region != `orgs.aws_region` → 401
      `sts_verification_failed` with category `region_mismatch`
    """
    if not request.payload:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "empty payload"},
        )

    if request.kind != "aws-sts":
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": f"unsupported kind: {request.kind!r}"},
        )

    source_ip = http_request.client.host if http_request.client is not None else None

    # Rate-limit keyed on source IP only.
    try:
        await check_identity_exchange(source_ip=source_ip)
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

    from app.core.agent_gateway.sts_verifier import (  # noqa: PLC0415
        InvalidSignedRequestError,
        extract_instance_id,
        verify_identity,
    )

    # Audience binding: the sigv4 envelope must carry a non-empty
    # `X-Yaaos-Audience` header matching this backend's canonical hostname
    # (`YAAOS_PUBLIC_HOSTNAME`). Checked by parsing the raw payload before
    # replay — avoids a live STS call for a mismatched request.
    try:
        import json as _json  # noqa: PLC0415

        parsed_payload = _json.loads(request.payload)
        headers_in_payload = parsed_payload.get("headers", {}) if isinstance(parsed_payload, dict) else {}
        # Normalize to lowercase keys (sigv4 headers are lowercase per spec).
        norm_headers = {k.lower(): v for k, v in headers_in_payload.items() if isinstance(k, str)}
        audience_in_payload = norm_headers.get("x-yaaos-audience", "")
    except Exception:
        audience_in_payload = ""

    # The expected audience is the server-side YAAOS_PUBLIC_HOSTNAME setting
    # (not the client-supplied Host header). Required — boot fails if unset.
    expected_audience = get_settings().yaaos_public_hostname

    if not audience_in_payload:
        log.warning(
            "identity_exchange.audience_missing",
            expected=expected_audience,
            source_ip=source_ip,
        )
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "audience_mismatch"},
        )

    if audience_in_payload != expected_audience:
        log.warning(
            "identity_exchange.audience_mismatch",
            expected=expected_audience,
            got=audience_in_payload,
            source_ip=source_ip,
        )
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "audience_mismatch"},
        )

    try:
        verified = await verify_identity(request.payload)
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

    org_ref = await lookup_org_by_arn(verified.canonical_arn)
    if org_ref is None:
        log.warning(
            "identity_exchange.arn_not_registered",
            canonical_arn=verified.canonical_arn,
            source_ip=source_ip,
        )
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "detail": "forbidden_unregistered_arn"},
        )
    org_id = org_ref.id
    org_region = org_ref.aws_region

    # Region pinning: the signed request must target the same AWS region the org
    # has on file. Defeats cross-region replay of a signature stolen from a
    # different STS endpoint.
    if org_region and verified.region != org_region:
        log.warning(
            "identity_exchange.region_mismatch",
            org_region=org_region,
            attempted_region=verified.region,
            source_ip=source_ip,
        )
        # Write an org-attributable audit row — the ARN matched a registered org
        # so we know which org to attribute this failure to.
        async with db_session() as s:
            await audit(
                entity_kind="org",
                entity_id=org_id,
                kind="identity_exchange_failed",
                payload=_IdentityExchangeFailedAudit(
                    category="region_mismatch",
                    attempted_arn=verified.canonical_arn,
                    source_ip=source_ip,
                ),
                actor=Actor(kind=ActorKind.SYSTEM),
                org_id=org_id,
                session=s,
            )
            await s.commit()
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "sts_verification_failed"},
        )

    # Derive the stable pod identifier from the role-session-name segment of the
    # raw assumed-role ARN. The backend learns the instance_id here; the agent
    # never supplies it.
    instance_id = extract_instance_id(verified.raw_arn)

    meta = request.agent_metadata
    async with db_session() as s:
        agent_id = await ensure_agent_row(
            org_id=org_id,
            instance_id=instance_id,
            iam_arn=verified.canonical_arn,
            version=request.agent_version or "0.0.1",
            session=s,
            os=meta.os,
            cpu_count=meta.cpu_count,
            memory_bytes=meta.memory_bytes,
        )
        plaintext, record = await bearers.issue(
            agent_id=agent_id,
            org_id=org_id,
            session=s,
            source_ip=source_ip,
            issued_iam_arn=verified.canonical_arn,
        )
        await s.commit()

    log.info(
        "identity_exchange.success",
        agent_id=str(agent_id),
        org_id=str(org_id),
        instance_id=instance_id,
        bearer_id=str(record.id),
    )

    from datetime import timedelta  # noqa: PLC0415

    renewal_after = record.expires_at - timedelta(minutes=5)

    return IdentityExchangeResponse(
        bearer=plaintext,
        expires_at=record.expires_at,
        renewal_after=renewal_after,
        agent_id=agent_id,
        instance_id=instance_id,
        org_id=org_id,
    )


@router.delete("/agent/identity")
async def deregister_identity(
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> Response:
    """Graceful-shutdown "going away" signal.

    The agent sends this as the last action of its SIGTERM/SIGINT handler,
    after stopping its heartbeat + claim loops and draining the WS. The
    control plane eagerly:

    1. Sets `workspace_agents.state=offline` + `last_shutdown_at=now`.
    2. Revokes the bearer so subsequent calls 401 immediately.
    3. Expires any workspaces owned by this agent and synthesizes terminal
       failure events for in-flight commands so their WorkflowExecutions resume.
    4. Publishes `agent_liveness_changed` SSE so the dashboard flips the card
       offline without waiting for the sweeper's next tick.

    Returns 204. Idempotent — calling on an already-offline/revoked agent is
    harmless (bearer verify fails → 401 before this handler runs).
    """
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        async with db_session() as s:
            # 1. Mark offline eagerly.
            await mark_agent_shutdown(agent.agent_id, session=s)

            # 2. Revoke this bearer immediately.
            await bearers.revoke(agent.bearer_id, "graceful_shutdown", session=s)

            # 3. Expire owned workspaces + synthesize terminal failures.
            await get_report_sink().handle_agent_loss({agent.agent_id}, s)

            # 4. SSE — cache-invalidate so the dashboard flips the card offline.
            publish_general_after_commit(
                s,
                org_id=agent.org_id,
                kind=GeneralEventKind.AGENT_LIVENESS_CHANGED,
                payload={},
            )

            await s.commit()

    log.info(
        "agent_gateway.graceful_shutdown",
        agent_id=str(agent.agent_id),
        org_id=str(agent.org_id),
    )
    return Response(status_code=204)


@router.post("/agent/heartbeat")
async def heartbeat(
    request: HeartbeatRequest,
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> HeartbeatResponse:
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        async with db_session() as s:
            response = await record_heartbeat(agent.agent_id, request, session=s)
            await s.commit()
    return response


@router.post("/agent/commands/claim")
async def claim_command(
    request: ClaimRequest,
    agent: bearers.BearerContext = Depends(_bearer_dep),
) -> Response:
    async with org_context(agent.org_id, ActorKind.WORKSPACE, actor_id=agent.agent_id):
        async with db_session() as s:
            command = await claim_next(
                agent.agent_id,
                lifecycle=request.lifecycle,
                new_workspaces=request.new_workspaces,
                workspace_ids=list(request.workspace_ids),
                wait_seconds=request.wait_seconds,
                session=s,
            )
            await s.commit()
        if command is None:
            return Response(status_code=204)
        return JSONResponse(status_code=200, content=command.model_dump(mode="json"))


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
                owning_agent_id = await get_report_sink().owning_agent_for_workspace(workspace_id, s)
                _require_workspace_owner(agent, owning_agent_id)
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
                owning_agent_id = await get_report_sink().owning_agent_for_command(command_id, s)
                _require_workspace_owner(agent, owning_agent_id)
                await record_agent_event(event, session=s)
                await s.commit()
        except StaleClaimError as exc:
            log.info("agent.command_event.stale", command_id=str(command_id), error=str(exc))
            return JSONResponse(status_code=410, content={"error": "stale_claim", "detail": str(exc)})
    return Response(status_code=200)


# ── Activity WebSocket ──────────────────────────────────────────────────


@router.websocket("/agent/activity")
async def activity_ws(websocket: WebSocket) -> None:
    """Bidirectional activity-stream channel. Agent identity is bearer-derived.

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
    await websocket.accept()

    agent_id = ctx.agent_id
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
