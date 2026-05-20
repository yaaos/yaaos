"""HTTP wiring for `domain/auth` — `/api/auth/*` endpoints.

`GET  /api/auth/login?provider=<id>&next=<path>` → 302 to the provider's
  authorization URL with a signed `state` carrying the optional post-login
  destination path.

`GET  /api/auth/callback/{provider}?code=...&state=...` → exchange `code`,
  verify the state signature, run `login_via_oauth`, set session + CSRF
  cookies, then 302 to the destination.

`POST /api/auth/logout` → revoke the current session and clear cookies.

Errors:
  - Hard reject (no matching user, no pending invitation) → 403 with body
    `{"error": "ask_for_invite", "email": <addr>}`.
  - Link challenge (email matches existing user, provider not linked) → 409
    with body `{"error": "link_required", "via_provider": ...}` and a signed
    `yaaos_link_pending` cookie. The SPA sends the user through the
    already-linked provider; the next callback completes the link.
  - Unknown provider → 404.
  - Unverified email from IdP → 403 `email_not_verified`.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from app.core.audit_log import Actor
from app.core.audit_log import audit as audit_write
from app.core.auth.cookies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    clear_cookie_attrs,
    csrf_cookie_attrs,
    session_cookie_attrs,
)
from app.core.auth.rate_limit import AUTH_LIMIT, MUTATE_LIMIT, limiter
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.domain.auth.dependencies import public_route
from app.domain.identity import sessions as session_lifecycle
from app.domain.identity.providers import (
    ProviderError,
    get_provider,
    list_providers,
)
from app.domain.identity.service import (
    HardRejectError,
    LinkChallengeRequiredError,
    complete_oauth_link,
    login_via_oauth,
)
from app.domain.identity.types import LinkChallengeRequiredError as _LCRE  # noqa: F401

log = structlog.get_logger("auth.web")

LINK_PENDING_COOKIE = "yaaos_link_pending"
TOTP_CHALLENGE_COOKIE = "yaaos_totp_challenge"
STATE_MAX_AGE_SECONDS = 600
LINK_PENDING_MAX_AGE_SECONDS = 600
TOTP_CHALLENGE_MAX_AGE_SECONDS = 600

router = APIRouter()


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().yaaos_oauth_state_secret, salt="yaaos-oauth-state")


def _link_pending_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().yaaos_oauth_state_secret, salt="yaaos-link-pending")


def _totp_challenge_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().yaaos_oauth_state_secret, salt="yaaos-totp-challenge")


def _redirect_uri_for(request: Request, provider_id: str) -> str:
    return f"{request.url.scheme}://{request.url.netloc}/api/auth/callback/{provider_id}"


def _safe_next(value: str | None) -> str:
    """Caller-supplied `next=` is honored only when it is a same-origin path.
    Anything else collapses to `/` to defeat open-redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("/login", dependencies=[Depends(public_route)])
@limiter.limit(AUTH_LIMIT)
async def login(
    request: Request,
    provider: Annotated[str, Query()],
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    p = get_provider(provider)
    if p is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_provider"})
    state = _state_serializer().dumps({"next": _safe_next(next), "provider": provider})
    return RedirectResponse(
        p.authorization_url(state=state, redirect_uri=_redirect_uri_for(request, provider))
    )


async def _revoke_pre_auth_session(s, request: Request) -> None:
    """If the user has any session cookie at the moment of login, revoke it.
    Implements the spec rule 'sessions rotated on login' — prevents a
    pre-auth session from being silently promoted into an authed one."""
    cookie = request.cookies.get("yaaos_session")
    if cookie:
        await session_lifecycle.revoke(s, cookie)


@router.get("/callback/{provider}", dependencies=[Depends(public_route)])
@limiter.limit(AUTH_LIMIT)
async def callback(
    request: Request,
    provider: str,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    yaaos_link_pending: Annotated[str | None, Cookie()] = None,
) -> Response:
    p = get_provider(provider)
    if p is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_provider"})
    try:
        state_payload = _state_serializer().loads(state, max_age=STATE_MAX_AGE_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail={"error": "state_expired"})
    except BadSignature:
        raise HTTPException(status_code=400, detail={"error": "state_invalid"})
    next_path = _safe_next(state_payload.get("next"))

    try:
        profile = await p.exchange_code(code=code, redirect_uri=_redirect_uri_for(request, provider))
    except ProviderError as exc:
        log.warning("auth.callback.provider_error", provider=provider, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "provider_error"})

    if not profile.email_verified:
        raise HTTPException(status_code=403, detail={"error": "email_not_verified"})

    # Link-confirm completion path: the user previously hit a link challenge,
    # we set the `yaaos_link_pending` cookie, they signed in via the already-
    # linked provider, and now we're attaching the new identity to their user.
    if yaaos_link_pending is not None:
        try:
            link_payload = _link_pending_serializer().loads(
                yaaos_link_pending, max_age=LINK_PENDING_MAX_AGE_SECONDS
            )
        except (BadSignature, SignatureExpired):
            link_payload = None
        if link_payload and link_payload.get("target_email") == profile.primary_email:
            async with db_session() as s:
                login_result = await login_via_oauth(s, provider_id=provider, profile=profile)
                await complete_oauth_link(
                    s,
                    user_id=login_result.user.id,
                    provider_id=link_payload["new_provider"],
                    external_subject=link_payload["new_external_subject"],
                )
                await _revoke_pre_auth_session(s, request)
                created = await session_lifecycle.create(
                    s,
                    user_id=login_result.user.id,
                    workspace_id=None,
                    ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
                await s.commit()
            resp = RedirectResponse(next_path, status_code=303)
            _set_session_cookies(resp, created)
            resp.set_cookie(**clear_cookie_attrs(LINK_PENDING_COOKIE))
            return resp

    try:
        async with db_session() as s:
            login_result = await login_via_oauth(s, provider_id=provider, profile=profile)

            # Step-up: if the user has a verified TOTP secret and the
            # provider didn't satisfy MFA, defer session creation and
            # send the user through `/totp-challenge`.
            from app.domain.identity import totp as totp_lifecycle  # noqa: PLC0415

            needs_step_up = not profile.mfa_satisfied and await totp_lifecycle.has_verified_totp(
                s, login_result.user.id
            )
            if needs_step_up:
                await s.commit()
                signed = _totp_challenge_serializer().dumps(
                    {"user_id": str(login_result.user.id), "next": next_path}
                )
                resp = JSONResponse(content={"step_up": "totp_required"})
                resp.set_cookie(
                    key=TOTP_CHALLENGE_COOKIE,
                    value=signed,
                    max_age=TOTP_CHALLENGE_MAX_AGE_SECONDS,
                    httponly=True,
                    samesite="lax",
                    secure=not get_settings().is_non_prod,
                    path="/",
                )
                return resp

            await _revoke_pre_auth_session(s, request)
            created = await session_lifecycle.create(
                s,
                user_id=login_result.user.id,
                workspace_id=None,
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            await _emit_login_audit(
                s,
                user_id=login_result.user.id,
                provider=provider,
                newly_created=login_result.newly_created,
            )
            await s.commit()
    except HardRejectError:
        return JSONResponse(
            status_code=403,
            content={"error": "ask_for_invite", "email": profile.primary_email},
        )
    except LinkChallengeRequiredError:
        payload = {
            "target_email": profile.primary_email,
            "new_provider": provider,
            "new_external_subject": profile.external_subject,
        }
        signed = _link_pending_serializer().dumps(payload)
        body = {
            "error": "link_required",
            "email": profile.primary_email,
            "via_provider": provider,
            "providers": [p_id for p_id in list_providers() if p_id != provider],
        }
        resp = JSONResponse(status_code=409, content=body)
        resp.set_cookie(
            key=LINK_PENDING_COOKIE,
            value=signed,
            max_age=LINK_PENDING_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=not get_settings().is_non_prod,
            path="/",
        )
        return resp

    resp = RedirectResponse(next_path, status_code=303)
    _set_session_cookies(resp, created)
    return resp


@router.post("/logout", dependencies=[Depends(public_route)])
@limiter.limit(MUTATE_LIMIT)
async def logout(
    request: Request,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    if yaaos_session:
        async with db_session() as s:
            session = await session_lifecycle.lookup(s, yaaos_session)
            if session and session.user_id is not None:
                await _emit_logout_audit(s, user_id=session.user_id, kind="logout")
            await session_lifecycle.revoke(s, yaaos_session)
            await s.commit()
    resp = JSONResponse(content={"ok": True})
    resp.set_cookie(**clear_cookie_attrs(SESSION_COOKIE_NAME))
    resp.set_cookie(**clear_cookie_attrs(CSRF_COOKIE_NAME))
    return resp


@router.post("/logout-all", dependencies=[Depends(public_route)])
@limiter.limit(MUTATE_LIMIT)
async def logout_all(
    request: Request,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Revoke every session for the user behind the current cookie. The
    /account page's 'Sign out everywhere' button hits this."""
    if not yaaos_session:
        resp = JSONResponse(content={"ok": True})
        resp.set_cookie(**clear_cookie_attrs(SESSION_COOKIE_NAME))
        resp.set_cookie(**clear_cookie_attrs(CSRF_COOKIE_NAME))
        return resp
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session and session.user_id is not None:
            await session_lifecycle.revoke_all_for_user(s, session.user_id)
        else:
            await session_lifecycle.revoke(s, yaaos_session)
        await s.commit()
    resp = JSONResponse(content={"ok": True})
    resp.set_cookie(**clear_cookie_attrs(SESSION_COOKIE_NAME))
    resp.set_cookie(**clear_cookie_attrs(CSRF_COOKIE_NAME))
    return resp


@router.get("/me", dependencies=[Depends(public_route)])
async def me(
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Return `{user, orgs, current_org_slug}` for the cookie-bearer.

    Lives on the public allowlist because the SPA hits it before the org
    is known; on success the SPA picks an org and sets `X-Org-Slug` on
    subsequent calls. 401 when there's no session.
    """
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.domain.identity import repository as identity_repo  # noqa: PLC0415
    from app.domain.integrations.models import McpCredentialRow  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415
    from app.domain.orgs.types import Role as _Role  # noqa: PLC0415

    if not yaaos_session:
        return JSONResponse(status_code=401, content={"error": "unauthenticated"})
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        user_row = await identity_repo.get_user(s, session.user_id)
        emails = await identity_repo.list_emails_for_user(s, session.user_id)
        memberships = await orgs_repo.list_memberships_for_user(s, session.user_id)
        orgs_view = []
        for m in memberships:
            org = await orgs_repo.get_org(s, m.org_id)
            if org is None:
                continue
            broken: list[dict[str, str | None]] = []
            # Owners + Admins see broken integrations; Members get an empty list.
            if _Role(m.role).covers(_Role.ADMIN):
                broken_rows = (
                    (
                        await s.execute(
                            _select(McpCredentialRow).where(
                                McpCredentialRow.org_id == org.id,
                                McpCredentialRow.enabled.is_(True),
                                McpCredentialRow.last_refresh_status == "failed",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                broken = [
                    {
                        "provider": r.provider,
                        "last_refresh_failed_at": (
                            r.last_refresh_failed_at.isoformat() if r.last_refresh_failed_at else None
                        ),
                    }
                    for r in broken_rows
                ]
            orgs_view.append(
                {
                    "slug": org.slug,
                    "display_name": org.display_name,
                    "role": m.role,
                    "handle": m.handle,
                    "broken_integrations": broken,
                }
            )
    primary_email = next((e.email for e in emails if e.is_primary), emails[0].email if emails else None)
    return JSONResponse(
        content={
            "user": {
                "id": str(session.user_id),
                "display_name": user_row.display_name if user_row else "",
                "primary_email": primary_email,
                "emails": [
                    {"email": e.email, "is_primary": e.is_primary, "verified": e.verified_at is not None}
                    for e in emails
                ],
            },
            "orgs": orgs_view,
            "current_org_slug": orgs_view[0]["slug"] if orgs_view else None,
        }
    )


@router.get("/providers", dependencies=[Depends(public_route)])
async def providers() -> dict[str, list[str]]:
    """List registered provider ids. The SPA renders one button per id on
    the login page; the test stub appears only when YAAOS_ENV=test."""
    return {"providers": list_providers()}


# ── M02 Phase 11 — TOTP enroll + verify ──────────────────────────────────


class _TotpVerifyRequest(BaseModel):
    code: str


@router.post("/totp/enroll", dependencies=[Depends(public_route)])
@limiter.limit(MUTATE_LIMIT)
async def totp_enroll(
    request: Request,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Mint a fresh (unverified) TOTP secret for the cookie-bearer. Returns
    `{seed, otpauth_uri}`. The SPA renders the URI as a QR code; users on
    devices without a camera type the seed. Verify must be called with a
    current code before `verified_at` flips."""
    from app.domain.identity import totp as totp_lifecycle  # noqa: PLC0415

    if not yaaos_session:
        return JSONResponse(status_code=401, content={"error": "unauthenticated"})
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        seed, uri = await totp_lifecycle.enroll(
            s, user_id=session.user_id, account_label=str(session.user_id)
        )
        await s.commit()
    return JSONResponse(content={"seed": seed, "otpauth_uri": uri})


@router.post("/totp/challenge", dependencies=[Depends(public_route)])
@limiter.limit(AUTH_LIMIT)
async def totp_challenge(
    body: _TotpVerifyRequest,
    request: Request,
    yaaos_totp_challenge: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Step-up endpoint: read the signed `yaaos_totp_challenge` cookie set
    by the OAuth callback when the user needs MFA, verify the supplied
    TOTP code, then mint the real session and redirect to the original
    `next` path."""
    from app.domain.identity import totp as totp_lifecycle  # noqa: PLC0415

    if not yaaos_totp_challenge:
        return JSONResponse(status_code=400, content={"error": "no_challenge_cookie"})
    try:
        payload = _totp_challenge_serializer().loads(
            yaaos_totp_challenge, max_age=TOTP_CHALLENGE_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return JSONResponse(status_code=400, content={"error": "challenge_invalid"})

    from uuid import UUID as _UUID  # noqa: PLC0415

    try:
        user_id = _UUID(payload["user_id"])
    except (KeyError, ValueError):
        return JSONResponse(status_code=400, content={"error": "challenge_invalid"})
    next_path = _safe_next(payload.get("next"))

    async with db_session() as s:
        ok = await totp_lifecycle.verify(s, user_id=user_id, code=body.code)
        if not ok:
            return JSONResponse(status_code=400, content={"error": "totp_invalid"})
        await _revoke_pre_auth_session(s, request)
        created = await session_lifecycle.create(
            s,
            user_id=user_id,
            workspace_id=None,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await _emit_login_audit(s, user_id=user_id, provider="totp_step_up", newly_created=False)
        await s.commit()

    resp = RedirectResponse(next_path, status_code=303)
    _set_session_cookies(resp, created)
    resp.set_cookie(**clear_cookie_attrs(TOTP_CHALLENGE_COOKIE))
    return resp


@router.post("/totp/verify", dependencies=[Depends(public_route)])
@limiter.limit(MUTATE_LIMIT)
async def totp_verify(
    request: Request,
    body: _TotpVerifyRequest,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> Response:
    """Verify a TOTP code against the user's enrolled secret. On success the
    row's `verified_at` stamp flips and step-up login starts demanding a
    code on every signin that wasn't satisfied by the IdP."""
    from app.domain.identity import totp as totp_lifecycle  # noqa: PLC0415

    if not yaaos_session:
        return JSONResponse(status_code=401, content={"error": "unauthenticated"})
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        ok = await totp_lifecycle.verify(s, user_id=session.user_id, code=body.code)
        await s.commit()
    if not ok:
        return JSONResponse(status_code=400, content={"error": "totp_invalid"})
    return JSONResponse(content={"ok": True})


def _set_session_cookies(resp: Response, created: session_lifecycle.CreatedSession) -> None:
    max_age = get_settings().yaaos_session_lifetime_seconds
    resp.set_cookie(value=created.raw_token, **session_cookie_attrs(max_age_seconds=max_age))
    resp.set_cookie(value=created.csrf_token, **csrf_cookie_attrs(max_age_seconds=max_age))


# Audit emission for user-global events (login, logout, link). The audit_log
# row schema requires `org_id`; we write one row per org the user is a member
# of. Users with zero memberships emit nothing — there's no org to attribute
# the event to.
class _LoginAuditPayload(BaseModel):
    provider: str
    newly_created: bool


class _LogoutAuditPayload(BaseModel):
    kind: str  # "logout" | "logout_all"


async def _emit_login_audit(s, *, user_id, provider: str, newly_created: bool) -> None:
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    memberships = await orgs_repo.list_memberships_for_user(s, user_id)
    actor = Actor.user(user_id=user_id)
    for m in memberships:
        await audit_write(
            "user",
            user_id,
            "logged_in",
            _LoginAuditPayload(provider=provider, newly_created=newly_created),
            actor,
            org_id=m.org_id,
            session=s,
        )


async def _emit_logout_audit(s, *, user_id, kind: str = "logout") -> None:
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    memberships = await orgs_repo.list_memberships_for_user(s, user_id)
    actor = Actor.user(user_id=user_id)
    for m in memberships:
        await audit_write(
            "user",
            user_id,
            kind,
            _LogoutAuditPayload(kind=kind),
            actor,
            org_id=m.org_id,
            session=s,
        )


register_routes(RouteSpec(module_name="auth", router=router))


__all__ = ["LINK_PENDING_COOKIE", "router"]
