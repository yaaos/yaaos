"""HTTP wiring for `domain/mcp_proxy` — Streamable HTTP MCP proxy.

The reviewer mints a per-review bearer via `mint_token(review_id, org_id=...)` and
writes it into the workspace's `.mcp.json`. The coding-agent CLI POSTs
JSON-RPC envelopes to `POST /api/mcp/{review_id}/{server}`; the proxy:

1. Authenticates the bearer via sha256-hash lookup (constant-time-safe).
2. Confirms `review_id` in the URL matches the token's review.
3. Reads `org_id` from the token row to look up the right provider
   credential via `domain/integrations.get(...)`.
4. Returns structured JSON-RPC errors for the three soft-failure cases —
   `not_connected` (no credential row), `broken_creds` (credential row
   exists but `last_refresh_status = "failed"`), `blocked_by_allowlist`
   (write tool not in the org's allowlist).
5. Forwards the JSON-RPC envelope to the upstream MCP server using the
   org's service-account access token.
6. Writes one `mcp.<provider>.dispatched` audit row per method call —
   never batched; one row per JSON-RPC method exercised.

Expired access tokens are not refreshed automatically: the proxy returns
`broken_creds` when the access token is expired, and the operator
reconnects through Org Settings > Integrations. The hourly health
check + email notification accelerate that loop in practice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.audit_log import Actor, audit
from app.core.auth import public_route
from app.core.database import session as db_session
from app.core.observability import spawn
from app.core.secrets import SecretsDecryptError, decrypt
from app.core.webserver import RouteSpec, register_routes
from app.domain.integrations import get, get_provider, get_secret, mark_last_used
from app.domain.mcp_proxy.service import lookup_token, record_broken_creds, run_sweep_loop

log = structlog.get_logger("mcp_proxy.web")


# JSON-RPC application-error range. -32000..-32099 is reserved by the spec
# for server-implementation errors; we layer human-readable `data.code`
# strings on top.
_RPC_ERR_NOT_CONNECTED = (-32001, "not_connected")
_RPC_ERR_BROKEN_CREDS = (-32002, "broken_creds")
_RPC_ERR_BLOCKED_BY_ALLOWLIST = (-32003, "blocked_by_allowlist")
_RPC_ERR_UNAUTHENTICATED = (-32004, "unauthenticated")
_RPC_ERR_UPSTREAM = (-32005, "upstream_error")
_UPSTREAM_TIMEOUT_SECONDS = 30.0


class _DispatchAuditPayload(BaseModel):
    provider: str
    method: str
    tool: str | None
    args_hash: str
    result_summary: str
    upstream_account: str = "org_service_account"


router = APIRouter()


def _rpc_error(rpc_id: Any, code: int, code_str: str, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message, "data": {"code": code_str}},
    }


def _args_hash(arguments: Any) -> str:
    try:
        canonical = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = "<unhashable>"
    return hashlib.sha256(canonical.encode()).hexdigest()


def _summarize(result: Any) -> str:
    """Compact one-line summary for the audit payload. We never store the
    full upstream payload — it can contain customer data."""
    if isinstance(result, dict):
        if "error" in result:
            return f"error:{result['error'].get('code')}"
        keys = sorted(result.keys())[:5]
        return f"ok:keys={','.join(keys)}"
    if isinstance(result, list):
        return f"ok:list:len={len(result)}"
    return f"ok:scalar:{type(result).__name__}"


@router.post("/{review_id}/{server}", dependencies=[Depends(public_route)])
async def dispatch(
    request: Request,
    review_id: Annotated[UUID, Path()],
    server: Annotated[str, Path()],
) -> JSONResponse:
    """The single proxy entry point. Bearer in `Authorization: Bearer ...`."""
    auth = request.headers.get("authorization", "")
    body = await request.json()
    rpc_id = body.get("id")

    if not auth.startswith("Bearer "):
        return JSONResponse(_rpc_error(rpc_id, *_RPC_ERR_UNAUTHENTICATED, "missing bearer"), status_code=401)
    raw_token = auth.removeprefix("Bearer ")

    async with db_session() as s:
        token_row = await lookup_token(raw_token, session=s)
        if token_row is None or token_row.review_id != review_id:
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_UNAUTHENTICATED, "invalid or mismatched token"),
                status_code=401,
            )

        # org_id is stored on the token row — no back-lookup into reviewer needed.
        org_id = token_row.org_id

        credential = await get(s, org_id, server)
        if credential is None or not credential.enabled:
            record_broken_creds(review_id, server)
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_NOT_CONNECTED, f"{server} not connected for org")
            )
        if credential.last_refresh_status == "failed":
            record_broken_creds(review_id, server)
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_BROKEN_CREDS, f"{server} credentials need reconnect")
            )
        if credential.expires_at < datetime.now(UTC):
            # Expired tokens aren't auto-refreshed; surface broken_creds.
            # The hourly health check flips last_refresh_status="failed"
            # so the UI surfaces it.
            record_broken_creds(review_id, server)
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    *_RPC_ERR_BROKEN_CREDS,
                    f"{server} access token expired; reconnect required",
                )
            )

        # Authorize the JSON-RPC method against the allowlist.
        provider_plugin = get_provider(server)
        method = body.get("method") or ""
        tool_name: str | None = None
        if method == "tools/call":
            tool_name = (body.get("params") or {}).get("name")
            if (
                provider_plugin is not None
                and tool_name in provider_plugin.config.known_write_tools
                and tool_name not in (credential.allowed_tools or [])
            ):
                return JSONResponse(
                    _rpc_error(
                        rpc_id,
                        *_RPC_ERR_BLOCKED_BY_ALLOWLIST,
                        f"{server}.{tool_name} not in allowlist",
                    )
                )

        # Fetch the secret separately (it never rides on the metadata VO) and
        # decrypt the upstream access token only at the call site.
        secret = await get_secret(s, org_id, server)
        if secret is None:
            record_broken_creds(review_id, server)
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_NOT_CONNECTED, f"{server} not connected for org")
            )
        try:
            upstream_token = decrypt(secret.encrypted_access_token.encode()).decode()
        except SecretsDecryptError:
            log.error("mcp_proxy.decrypt_failed", provider=server)
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_BROKEN_CREDS, f"{server} credentials unreadable")
            )

        # Forward to the upstream MCP server. Provider plugin config has
        # the mcp_url; the upstream Authorization header carries the org
        # service-account access token (the per-review yaaos bearer never
        # leaves yaaos).
        if provider_plugin is None:
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_NOT_CONNECTED, f"{server} provider not registered")
            )
        try:
            async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_SECONDS) as http:
                upstream_resp = await http.post(
                    provider_plugin.config.mcp_url,
                    headers={"Authorization": f"Bearer {upstream_token}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            log.warning("mcp_proxy.upstream_transport_error", error=str(exc), provider=server)
            return JSONResponse(_rpc_error(rpc_id, *_RPC_ERR_UPSTREAM, "upstream transport error"))
        if upstream_resp.status_code >= 500:
            return JSONResponse(
                _rpc_error(rpc_id, *_RPC_ERR_UPSTREAM, f"upstream {upstream_resp.status_code}")
            )

        # Stamp last_used_at; the UI surfaces "last used X minutes ago".
        await mark_last_used(s, org_id=org_id, provider=server)

        # One audit row per dispatched method.
        try:
            result_payload = upstream_resp.json()
        except ValueError:
            result_payload = {"raw": upstream_resp.text[:200]}
        result_summary = _summarize(result_payload.get("result", result_payload))

        await audit(
            "org",
            org_id,
            f"mcp.{server}.dispatched",
            _DispatchAuditPayload(
                provider=server,
                method=method,
                tool=tool_name,
                args_hash=_args_hash((body.get("params") or {}).get("arguments")),
                result_summary=result_summary,
            ),
            Actor.system(),
            org_id=org_id,
            session=s,
        )
        await s.commit()

    return JSONResponse(result_payload)


async def _start_sweep() -> None:
    spawn("mcp_proxy.sweep", run_sweep_loop())


register_routes(RouteSpec(module_name="mcp", router=router, url_prefix="/api/mcp", on_startup=[_start_sweep]))
