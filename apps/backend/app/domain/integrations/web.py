"""HTTP wiring for `domain/integrations`.

| Method | Path                                       | Action               |
|--------|--------------------------------------------|----------------------|
| GET    | `/api/integrations/broken-summary`      | cookie-auth (public_route) — cross-org broken-creds summary for the caller |
| GET    | `/api/mcp-proxy`                        | `INTEGRATIONS_READ`  — list providers + status |
| GET    | `/api/mcp-proxy/{provider}/connect`     | `INTEGRATIONS_WRITE` — 303 to provider authorize URL with signed state |
| GET    | `/api/mcp-proxy/{provider}/callback`    | public_route         — OAuth redirect target; validates signed state |
| POST   | `/api/mcp-proxy/{provider}/validate`    | `INTEGRATIONS_WRITE` — hit upstream with stored token |
| PATCH  | `/api/mcp-proxy/{provider}`             | `INTEGRATIONS_WRITE` — update `allowed_tools` + `enabled` |
| DELETE | `/api/mcp-proxy/{provider}`             | `INTEGRATIONS_WRITE` — clear |

Architecture writes paths as `/api/orgs/{slug}/integrations/{provider}/...`
for human readability; the working implementation mirrors other settings endpoints (vcs, coding-agents, byok) — slug comes via the `X-Yaaos-Org-Slug`
header so the SPA's `apiFetch` wrapper carries it automatically.

The callback path is the exception: it's hit by the upstream OAuth provider,
which doesn't know about our header. The signed `state` parameter carries
the `org_id` + `user_initiating` so the callback can resolve everything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import select

from app.core.audit_log import Actor
from app.core.auth import Action, Role, org_id_var, public_route, user_id_var
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.oauth import OAuthError, build_authorize_url
from app.core.observability import spawn
from app.core.sessions import current_actor, require
from app.core.webserver import RouteSpec, register_routes
from app.domain.integrations import service as integ
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.scheduler import run_scheduler_loop
from app.domain.integrations.types import (
    IntegrationNotConnectedError,
    ProviderNotRegisteredError,
    get_provider,
    known_providers,
)

log = structlog.get_logger("integrations.web")

router = APIRouter()
summary_router = APIRouter()


_STATE_MAX_AGE_SECONDS = 600
_STATE_SALT = "yaaos-integration-connect"


def _state_serializer() -> URLSafeTimedSerializer:
    # Reuses the invitation-token secret — same lifecycle (server-side
    # rotation), same operator pager rotation discipline.
    return URLSafeTimedSerializer(
        get_settings().yaaos_invitation_token_secret.get_secret_value(), salt=_STATE_SALT
    )


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{request.url.scheme}://{request.url.netloc}/api/mcp-proxy/{provider}/callback"


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


class IntegrationStatus(BaseModel):
    provider: str
    status: str  # "configured" | "not_set" | "broken"
    enabled: bool | None = None
    upstream_identity: str | None = None
    last_validated_at: datetime | None = None
    last_refresh_failed_at: datetime | None = None
    allowed_tools: list[str] = []


class PatchIntegrationRequest(BaseModel):
    allowed_tools: list[str] | None = None
    enabled: bool | None = None


@router.get("", dependencies=[Depends(require(Action.INTEGRATIONS_READ))])
async def list_integrations() -> list[IntegrationStatus]:
    """Returns one row per registered provider so the UI can render the full
    picker even before anything is connected. Unconnected providers come
    back as `status="not_set"`."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")

    async with db_session() as s:
        rows = {
            r.provider: r
            for r in (await s.execute(select(McpCredentialRow).where(McpCredentialRow.org_id == org_id)))
            .scalars()
            .all()
        }
    out: list[IntegrationStatus] = []
    for prov_id in known_providers():
        r = rows.get(prov_id)
        if r is None:
            out.append(IntegrationStatus(provider=prov_id, status="not_set"))
            continue
        status = "broken" if r.last_refresh_status == "failed" else "configured"
        out.append(
            IntegrationStatus(
                provider=prov_id,
                status=status,
                enabled=r.enabled,
                upstream_identity=r.upstream_identity,
                last_validated_at=r.last_validated_at,
                last_refresh_failed_at=r.last_refresh_failed_at,
                allowed_tools=list(r.allowed_tools or []),
            )
        )
    return out


@router.get(
    "/{provider}/connect",
    dependencies=[Depends(require(Action.INTEGRATIONS_WRITE))],
)
async def connect_start(request: Request, provider: str) -> RedirectResponse:
    """Mint a signed `state` carrying `(org_id, user_initiating)` and 303 to
    the provider's authorize URL. The callback verifies the signature + uses
    the embedded org_id (since the upstream doesn't know our X-Yaaos-Org-Slug)."""
    org_id = org_id_var.get()
    user_id = user_id_var.get()
    if org_id is None or user_id is None:
        raise _err(400, "no_org_context")
    prov = get_provider(provider)
    if prov is None:
        raise _err(404, "unknown_provider")
    state = _state_serializer().dumps(
        {"org_id": str(org_id), "user_initiating": str(user_id), "provider": provider}
    )
    url = build_authorize_url(
        prov.config,
        state=state,
        redirect_uri=_redirect_uri(request, provider),
    )
    return RedirectResponse(url, status_code=303)


@router.get("/{provider}/callback", dependencies=[Depends(public_route)])
async def connect_callback(
    request: Request,
    provider: str,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
) -> RedirectResponse:
    """OAuth redirect target. Validates the signed state, exchanges the code,
    persists the credential, redirects the operator back to the Integrations
    settings page."""
    try:
        payload: dict[str, Any] = _state_serializer().loads(state, max_age=_STATE_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise _err(400, "state_expired") from exc
    except BadSignature as exc:
        raise _err(400, "state_invalid") from exc
    if payload.get("provider") != provider:
        raise _err(400, "state_provider_mismatch")
    try:
        org_id = UUID(payload["org_id"])
        user_id = UUID(payload["user_initiating"])
    except (KeyError, ValueError) as exc:
        raise _err(400, "state_invalid") from exc

    actor = Actor.user(user_id=user_id)
    try:
        async with db_session() as s:
            await integ.connect_callback(
                s,
                provider=provider,
                code=code,
                org_id=org_id,
                redirect_uri=_redirect_uri(request, provider),
                actor=actor,
            )
            await s.commit()
    except ProviderNotRegisteredError as exc:
        raise _err(404, "unknown_provider") from exc
    except OAuthError as exc:
        log.warning("integrations.callback.oauth_error", provider=provider, error=str(exc))
        raise _err(502, "oauth_error") from exc

    # Redirect the operator back to the Integrations UI. The slug isn't on
    # the callback's URL; the SPA's index probe will land them in the right
    # org. We don't have direct org-slug-from-id lookup here without an
    # extra query, so route through the SPA root.
    return RedirectResponse("/", status_code=303)


@router.post(
    "/{provider}/validate",
    dependencies=[Depends(require(Action.INTEGRATIONS_WRITE))],
)
async def validate_endpoint(provider: str) -> dict[str, bool]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        try:
            ok = await integ.validate(s, org_id=org_id, provider=provider, actor=actor)
            await s.commit()
        except ProviderNotRegisteredError as exc:
            raise _err(404, "unknown_provider") from exc
        except IntegrationNotConnectedError as exc:
            raise _err(404, "not_connected") from exc
    return {"valid": ok}


@router.patch("/{provider}", dependencies=[Depends(require(Action.INTEGRATIONS_WRITE))])
async def patch_integration(provider: str, body: PatchIntegrationRequest) -> IntegrationStatus:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        cred = await integ.get(s, org_id, provider)
        if cred is None:
            raise _err(404, "not_connected")
        if body.enabled is not None:
            from app.domain.integrations.service import _get_row as _get_cred_row  # noqa: PLC0415

            raw_row = await _get_cred_row(s, org_id, provider)
            if raw_row is not None:
                raw_row.enabled = body.enabled
        if body.allowed_tools is not None:
            try:
                cred = await integ.update_allowlist(
                    s,
                    org_id=org_id,
                    provider=provider,
                    allowed_tools=body.allowed_tools,
                    actor=actor,
                )
            except IntegrationNotConnectedError as exc:
                raise _err(404, "not_connected") from exc
        await s.commit()
        # Re-read to pick up all committed state.
        cred = await integ.get(s, org_id, provider) or cred
    status = "broken" if cred.last_refresh_status == "failed" else "configured"
    return IntegrationStatus(
        provider=provider,
        status=status,
        enabled=cred.enabled,
        upstream_identity=cred.upstream_identity,
        last_validated_at=None,
        last_refresh_failed_at=cred.last_refresh_failed_at,
        allowed_tools=list(cred.allowed_tools or []),
    )


@router.delete("/{provider}", dependencies=[Depends(require(Action.INTEGRATIONS_WRITE))])
async def clear_endpoint(provider: str) -> dict[str, bool]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        removed = await integ.clear(s, org_id=org_id, provider=provider, actor=actor)
        await s.commit()
    return {"removed": removed}


@summary_router.get("/broken-summary", dependencies=[Depends(public_route)])
async def broken_summary(
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Return per-org broken-credential counts for the cookie-bearer.

    Response: `{ orgs: [{ org_id, broken_integrations: [{ provider }] }] }`.
    Only Owners + Admins receive non-empty `broken_integrations` lists; Builder-role
    memberships always yield an empty list. 401 when there is no valid session.
    """
    from fastapi.responses import JSONResponse as _JSONResponse  # noqa: PLC0415

    from app.core.auth import auth_failure_response as _auth_failure  # noqa: PLC0415
    from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415
    from app.core.tenancy import list_memberships_for_user as _list_memberships  # noqa: PLC0415

    if not yaaos_session:
        return _auth_failure("unauthenticated")
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return _auth_failure("unauthenticated")
        memberships = await _list_memberships(s, session.user_id)
        orgs_out = []
        for m in memberships:
            broken: list[dict[str, str]] = []
            if m.role.covers(Role.ADMIN):
                broken_creds = await integ.list_broken_credentials_for_org(s, m.org_id)
                broken = [{"provider": c.provider} for c in broken_creds]
            orgs_out.append({"org_id": str(m.org_id), "broken_integrations": broken})

    return _JSONResponse(content={"orgs": orgs_out})


async def _start_scheduler() -> None:
    spawn("integrations.scheduler", run_scheduler_loop())


register_routes(
    RouteSpec(
        module_name="integrations_summary",
        router=summary_router,
        url_prefix="/api/integrations",
    )
)

register_routes(
    RouteSpec(
        module_name="integrations",
        router=router,
        url_prefix="/api/mcp-proxy",
        on_startup=[_start_scheduler],
    )
)
