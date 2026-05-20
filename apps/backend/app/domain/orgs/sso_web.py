"""HTTP wiring for `/api/sso/*` — per-org SAML SSO endpoints.

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/sso/{slug}/metadata`  | public; returns SP metadata XML for the org. |
| GET    | `/api/sso/{slug}/login`     | public; starts an SP-initiated SAML AuthnRequest. |
| POST   | `/api/sso/{slug}/acs`       | public; ACS — verifies assertion, marks session SSO-satisfied. |
| GET    | `/api/sso/config`           | Owner-only; current per-org SSO config (no SP private key). |
| PUT    | `/api/sso/config`           | Owner-only; upsert IdP metadata + JIT toggle + exempt-owner.    |
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.core.audit_log import Actor
from app.core.audit_log import audit as audit_write
from app.core.auth.context import org_id_var
from app.core.auth.rate_limit import AUTH_LIMIT, MUTATE_LIMIT, limiter
from app.core.auth.types import Action
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("orgs.sso.web")

router = APIRouter()


class _SsoConfigBody(BaseModel):
    idp_metadata_xml: str
    jit_enabled: bool = False
    enabled: bool = False
    exempt_owner_user_id: UUID | None = None


class _AssertionBody(BaseModel):
    SAMLResponse: str


class _SsoSatisfiedAuditPayload(BaseModel):
    email: str
    jit_created: bool


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("/{slug}/metadata")
async def sp_metadata(slug: str) -> Response:
    """Public endpoint — operators hand this URL to the IdP."""
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415
    from app.domain.orgs.sso import sp_metadata_xml  # noqa: PLC0415

    async with db_session() as s:
        org = await orgs_repo.get_org_by_slug(s, slug)
    if org is None:
        raise _err(404, "org_not_found")
    base_url = get_settings().yaaos_app_base_url
    xml = sp_metadata_xml(slug, base_url)
    return Response(content=xml, media_type="application/xml")


@router.get("/{slug}/login")
async def sso_login_start(slug: str) -> Response:
    """SP-initiated login. In POC test env we redirect to the test stub IdP
    page (which the test then POSTs back via `/acs`). Production builds the
    AuthnRequest XML via `plugins/saml`."""
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415
    from app.domain.orgs.sso import get_config  # noqa: PLC0415

    async with db_session() as s:
        org = await orgs_repo.get_org_by_slug(s, slug)
        if org is None:
            raise _err(404, "org_not_found")
        cfg = await get_config(s, org_id=org.id)
    if cfg is None or not cfg.enabled:
        raise _err(404, "sso_not_configured")
    # POC redirect target: the SPA's hand-off page. Tests POST directly to /acs.
    return RedirectResponse(f"/login?sso_for={slug}")


@router.post("/{slug}/acs")
@limiter.limit(AUTH_LIMIT)
async def sso_acs(
    request: Request,
    slug: str,
    body: _AssertionBody,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Assertion Consumer Service. Verifies the SAML response (real or stub),
    matches the user by verified email, optionally JIT-creates a membership,
    marks the session SSO-satisfied for this org."""
    from app.domain.identity import repository as identity_repo  # noqa: PLC0415
    from app.domain.identity import sessions as session_lifecycle  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415
    from app.domain.orgs.sso import get_config  # noqa: PLC0415
    from app.domain.orgs.types import Role  # noqa: PLC0415

    async with db_session() as s:
        org = await orgs_repo.get_org_by_slug(s, slug)
        if org is None:
            raise _err(404, "org_not_found")
        cfg = await get_config(s, org_id=org.id)
        if cfg is None or not cfg.enabled:
            raise _err(404, "sso_not_configured")

        payload = _verify_assertion(body.SAMLResponse, cfg.idp_metadata_xml)
        if payload is None or not payload.get("email"):
            raise _err(400, "assertion_invalid")
        email = payload["email"].lower()

        # Match by verified email.
        user_row = await identity_repo.find_user_by_email(s, email)
        jit_created = False
        if user_row is None:
            if not cfg.jit_enabled:
                raise _err(403, "user_not_provisioned")
            user_row = await identity_repo.insert_user(s, display_name=email.split("@")[0])
            await identity_repo.add_email(s, user_id=user_row.id, email=email, is_primary=True, verified=True)
            await orgs_repo.insert_membership(
                s,
                user_id=user_row.id,
                org_id=org.id,
                role=Role.MEMBER,
                handle=email.split("@")[0][:64].lower(),
            )
            jit_created = True

        membership = await orgs_repo.get_membership(s, user_id=user_row.id, org_id=org.id)
        if membership is None:
            raise _err(403, "no_membership")

        # If the user already has a session, mark it SSO-satisfied. Otherwise
        # mint a fresh session.
        if yaaos_session:
            try:
                updated = await session_lifecycle.mark_sso_satisfied(s, yaaos_session, org_id=org.id)
                created = None
            except Exception:
                updated = None
                created = await session_lifecycle.create(s, user_id=user_row.id, workspace_id=None)
                await session_lifecycle.mark_sso_satisfied(s, created.raw_token, org_id=org.id)
        else:
            created = await session_lifecycle.create(s, user_id=user_row.id, workspace_id=None)
            await session_lifecycle.mark_sso_satisfied(s, created.raw_token, org_id=org.id)
            updated = None

        await audit_write(
            "user",
            user_row.id,
            "sso_satisfied",
            _SsoSatisfiedAuditPayload(email=email, jit_created=jit_created),
            Actor(kind="sso", login=email),
            org_id=org.id,
            session=s,
        )

        # Break-glass: if the user is the exempt Owner and they reached
        # this code path via OAuth+TOTP (not the SSO IdP), the require()
        # bypass branch will have written nothing — but a direct SSO
        # signin still counts as satisfaction. Capture the break-glass
        # case via a separate audit row when middleware lets exempt
        # bypass. Marker emitted in `domain/auth/dependencies` is the
        # source of truth for "Owner skipped SSO".
        await s.commit()

    next_path = f"/orgs/{slug}/dashboard"
    resp = RedirectResponse(next_path, status_code=303)
    if created is not None:
        from app.core.auth.cookies import csrf_cookie_attrs, session_cookie_attrs  # noqa: PLC0415

        max_age = get_settings().yaaos_session_lifetime_seconds
        resp.set_cookie(value=created.raw_token, **session_cookie_attrs(max_age_seconds=max_age))
        resp.set_cookie(value=created.csrf_token, **csrf_cookie_attrs(max_age_seconds=max_age))
    _ = updated  # currently unused; placeholder for future telemetry
    return resp


def _verify_assertion(saml_response: str, idp_metadata_xml: str) -> dict | None:
    """Dispatch via the assertion-verifier registry. Plugins push their
    verifiers into `domain.orgs.sso` at import time."""
    from app.domain.orgs.sso import run_assertion_verifier  # noqa: PLC0415

    return run_assertion_verifier(saml_response, idp_metadata_xml)


# ── Owner-only config CRUD ──────────────────────────────────────────────


def _require_sso_configure():
    from app.domain.auth.dependencies import require  # noqa: PLC0415

    return require(Action.SSO_CONFIGURE)


@router.get("/config", dependencies=[Depends(_require_sso_configure())])
async def get_org_sso_config() -> dict:
    from app.domain.orgs.sso import get_config  # noqa: PLC0415

    org_id = org_id_var.get()
    assert org_id is not None
    async with db_session() as s:
        cfg = await get_config(s, org_id=org_id)
    if cfg is None:
        return {"enabled": False, "jit_enabled": False, "exempt_owner_user_id": None}
    return {
        "enabled": cfg.enabled,
        "jit_enabled": cfg.jit_enabled,
        "exempt_owner_user_id": str(cfg.exempt_owner_user_id) if cfg.exempt_owner_user_id else None,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


@router.put("/config", dependencies=[Depends(_require_sso_configure())])
@limiter.limit(MUTATE_LIMIT)
async def upsert_org_sso_config(request: Request, body: _SsoConfigBody) -> dict:
    """Upsert per-org SSO config. The exempt-Owner picker requires the
    candidate to have a verified TOTP secret — otherwise reject with
    `exempt_owner_no_totp`. Phase 11 helper enforces. Writes a
    `sso_config_changed` audit row + an `exempt_owner_set` row when
    the exempt-Owner pointer changed."""
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.core.auth.context import user_id_var  # noqa: PLC0415
    from app.domain.auth.dependencies import current_actor  # noqa: PLC0415
    from app.domain.identity.totp import can_be_sso_exempt_owner  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415
    from app.domain.orgs.sso import SsoConfigError, get_config, upsert_config  # noqa: PLC0415

    org_id = org_id_var.get()
    assert org_id is not None
    actor_user_id = user_id_var.get()
    actor = current_actor() if actor_user_id else Actor(kind="system")

    async with db_session() as s:
        if body.exempt_owner_user_id is not None:
            membership = await orgs_repo.get_membership(s, user_id=body.exempt_owner_user_id, org_id=org_id)
            if membership is None or membership.role != "owner":
                raise _err(400, "exempt_must_be_owner")
            if not await can_be_sso_exempt_owner(s, body.exempt_owner_user_id):
                raise _err(400, "exempt_owner_no_totp")
        previous = await get_config(s, org_id=org_id)
        prev_exempt = previous.exempt_owner_user_id if previous else None
        prev_enabled = previous.enabled if previous else False
        prev_jit = previous.jit_enabled if previous else False
        try:
            cfg = await upsert_config(
                s,
                org_id=org_id,
                idp_metadata_xml=body.idp_metadata_xml,
                jit_enabled=body.jit_enabled,
                enabled=body.enabled,
                exempt_owner_user_id=body.exempt_owner_user_id,
            )
        except SsoConfigError as exc:
            raise _err(400, str(exc))
        await _emit_sso_config_audit(
            s,
            org_id=org_id,
            actor=actor,
            prev_enabled=prev_enabled,
            new_enabled=cfg.enabled,
            prev_jit=prev_jit,
            new_jit=cfg.jit_enabled,
            prev_exempt=prev_exempt,
            new_exempt=cfg.exempt_owner_user_id,
        )
        await s.commit()
    return {
        "enabled": cfg.enabled,
        "jit_enabled": cfg.jit_enabled,
        "exempt_owner_user_id": (str(cfg.exempt_owner_user_id) if cfg.exempt_owner_user_id else None),
        "updated_at": datetime.now(UTC).isoformat(),
    }


class _SsoConfigAuditPayload(BaseModel):
    enabled: bool
    jit_enabled: bool
    exempt_owner_user_id: UUID | None
    changed_enabled: bool = False
    changed_jit: bool = False
    changed_exempt_owner: bool = False


async def _emit_sso_config_audit(
    s,
    *,
    org_id: UUID,
    actor,
    prev_enabled: bool,
    new_enabled: bool,
    prev_jit: bool,
    new_jit: bool,
    prev_exempt: UUID | None,
    new_exempt: UUID | None,
) -> None:
    from app.core.audit_log import audit  # noqa: PLC0415

    payload = _SsoConfigAuditPayload(
        enabled=new_enabled,
        jit_enabled=new_jit,
        exempt_owner_user_id=new_exempt,
        changed_enabled=prev_enabled != new_enabled,
        changed_jit=prev_jit != new_jit,
        changed_exempt_owner=prev_exempt != new_exempt,
    )
    await audit("sso_config", org_id, "sso_config_changed", payload, actor, org_id=org_id, session=s)
    if prev_exempt != new_exempt and new_exempt is not None:
        await audit(
            "sso_config",
            org_id,
            "exempt_owner_set",
            _SsoConfigAuditPayload(
                enabled=new_enabled,
                jit_enabled=new_jit,
                exempt_owner_user_id=new_exempt,
                changed_exempt_owner=True,
            ),
            actor,
            org_id=org_id,
            session=s,
        )


register_routes(RouteSpec(module_name="sso", router=router, url_prefix="/api/sso"))


__all__ = ["router"]
