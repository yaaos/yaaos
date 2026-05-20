"""HTTP wiring for `domain/integrations`.

| Method | Path                                       | Action               |
|--------|--------------------------------------------|----------------------|
| GET    | `/api/integrations`                        | `INTEGRATIONS_READ`  — list providers + status |
| GET    | `/api/integrations/{provider}/connect`     | `INTEGRATIONS_WRITE` — 303 to provider authorize URL with signed state |
| GET    | `/api/integrations/{provider}/callback`    | public_route         — OAuth redirect target; validates signed state |
| POST   | `/api/integrations/{provider}/validate`    | `INTEGRATIONS_WRITE` — hit upstream with stored token |
| PATCH  | `/api/integrations/{provider}`             | `INTEGRATIONS_WRITE` — update `allowed_tools` + `enabled` |
| DELETE | `/api/integrations/{provider}`             | `INTEGRATIONS_WRITE` — clear |

Architecture writes paths as `/api/orgs/{slug}/integrations/{provider}/...`
for human readability; the working implementation mirrors other M03/M04
endpoints (vcs, coding-agents, byok) — slug comes via the `X-Org-Slug`
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
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import select

from app.core.audit_log import Actor
from app.core.auth import public_route
from app.core.auth.context import org_id_var, user_id_var
from app.core.auth.types import Action
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.oauth import OAuthError, build_authorize_url
from app.core.observability import spawn
from app.core.webserver import RouteSpec, register_routes
from app.domain.auth.dependencies import current_actor, require
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


_STATE_MAX_AGE_SECONDS = 600
_STATE_SALT = "yaaos-integration-connect"


def _state_serializer() -> URLSafeTimedSerializer:
    # Reuses the M02 invitation-token secret — same lifecycle (server-side
    # rotation), same operator pager rotation discipline.
    return URLSafeTimedSerializer(get_settings().yaaos_invitation_token_secret, salt=_STATE_SALT)


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{request.url.scheme}://{request.url.netloc}/api/integrations/{provider}/callback"


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
    the embedded org_id (since the upstream doesn't know our X-Org-Slug)."""
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
        row = await integ.get(s, org_id, provider)
        if row is None:
            raise _err(404, "not_connected")
        if body.enabled is not None:
            row.enabled = body.enabled
        if body.allowed_tools is not None:
            try:
                await integ.update_allowlist(
                    s,
                    org_id=org_id,
                    provider=provider,
                    allowed_tools=body.allowed_tools,
                    actor=actor,
                )
            except IntegrationNotConnectedError as exc:
                raise _err(404, "not_connected") from exc
        await s.commit()
        await s.refresh(row)
    status = "broken" if row.last_refresh_status == "failed" else "configured"
    return IntegrationStatus(
        provider=provider,
        status=status,
        enabled=row.enabled,
        upstream_identity=row.upstream_identity,
        last_validated_at=row.last_validated_at,
        last_refresh_failed_at=row.last_refresh_failed_at,
        allowed_tools=list(row.allowed_tools or []),
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


async def _start_scheduler() -> None:
    spawn("integrations.scheduler", run_scheduler_loop())


register_routes(
    RouteSpec(
        module_name="integrations",
        router=router,
        url_prefix="/api/integrations",
        on_startup=[_start_scheduler],
    )
)
