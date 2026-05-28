"""HTTP wiring for `core/sessions` — `/api/auth/*` endpoints.

`GET  /api/auth/login?provider=<id>&next=<path>` → 302 to the provider's
  authorization URL with a signed `state` carrying the optional post-login
  destination path.

`GET  /api/auth/callback/{provider}?code=...&state=...` → exchange `code`,
  verify the state signature, run `login_via_oauth`, set session + CSRF
  cookies, then 302 to the destination.

`POST /api/auth/logout` → revoke the current session and clear cookies.

Errors:
  - Unknown provider → 404.
  - Unverified email from IdP → 403 `email_not_verified`.
  - Provider transport failure → 502 `provider_error`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from app.core.audit_log import Actor
from app.core.audit_log import audit as audit_write
from app.core.auth import (
    AUTH_LIMIT,
    CSRF_COOKIE_NAME,
    MUTATE_LIMIT,
    SESSION_COOKIE_NAME,
    auth_failure_response,
    clear_cookie_attrs,
    csrf_cookie_attrs,
    limiter,
    session_cookie_attrs,
)
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.identity import (
    ProviderError,
    get_provider,
    list_providers,
    login_via_oauth,
)
from app.core.identity import (
    sessions as session_lifecycle,
)
from app.core.sessions.dependencies import public_route
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("auth.web")

TOTP_CHALLENGE_COOKIE = "yaaos_totp_challenge"
STATE_MAX_AGE_SECONDS = 600
TOTP_CHALLENGE_MAX_AGE_SECONDS = 600

router = APIRouter()


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().yaaos_oauth_state_secret.get_secret_value(), salt="yaaos-oauth-state"
    )


def _totp_challenge_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().yaaos_oauth_state_secret.get_secret_value(), salt="yaaos-totp-challenge"
    )


def _redirect_uri_for(request: Request, provider_id: str) -> str:
    return f"{request.url.scheme}://{request.url.netloc}/api/auth/callback/{provider_id}"


def _safe_next(value: str | None) -> str:
    """Caller-supplied `next=` is honored only when it is a same-origin path.
    Anything else collapses to `/` to defeat open-redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


_ORG_SLUG_RE = re.compile(r"^/orgs/([^/]+)(/|$)")


async def _safe_next_for_user(s, value: str | None, *, user_id) -> str:
    """`_safe_next` plus membership validation. If the path points at
    `/orgs/$slug/...`, require `user_id` to have a membership in `$slug`;
    otherwise collapse to `/`. Prevents post-login redirects to orgs the
    user no longer belongs to (or never did). Same allowlist semantics as
    `_safe_next` otherwise.
    """
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    path = _safe_next(value)
    m = _ORG_SLUG_RE.match(path)
    if not m:
        return path
    slug = m.group(1)
    if not slug or slug in {"undefined", "null"}:
        return "/"
    org = await orgs_repo.get_org_by_slug(s, slug)
    if org is None:
        return "/"
    membership = await orgs_repo.get_membership(s, user_id=user_id, org_id=org.id)
    if membership is None:
        return "/"
    return path


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

    async with db_session() as s:
        login_result = await login_via_oauth(s, provider_id=provider, profile=profile)

        # No matching yaaos user (neither by (provider, external_subject) nor
        # by verified email). OAuth never auto-provisions; the user must be
        # invited first. Drop them on /login with a banner and no cookie.
        if login_result.user is None:
            await s.rollback()
            log.info(
                "auth.callback.not_provisioned",
                provider=provider,
                external_subject=profile.external_subject,
                primary_email=profile.primary_email,
            )
            return RedirectResponse("/login?reason=not_provisioned", status_code=303)

        # Step-up: if the user has a verified TOTP secret and the provider
        # didn't satisfy MFA, defer session creation and send the user
        # through `/totp-challenge`.
        from app.core.identity import totp as totp_lifecycle  # noqa: PLC0415

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
        # Validate next now that we know who's signing in. If next points
        # at /orgs/$slug/... but the user has no membership in $slug, fall
        # back to `/` (indexRoute will route them to a real destination).
        next_path = await _safe_next_for_user(s, next_path, user_id=login_result.user.id)
        await s.commit()

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
    """Return `{user, memberships}` for the cookie-bearer.

    `memberships` is the authenticated user's current memberships (revoked
    ones disappear next call). The server has no opinion about which org is
    "current" — that's view state and lives in the URL.

    Lives on the public allowlist because the SPA hits it before any org
    URL is selected; on routes that need it, the SPA adds `X-Org-Slug` from
    the URL path. 401 when there's no session.
    """
    from app.core.identity import repository as identity_repo  # noqa: PLC0415
    from app.domain.integrations import list_broken_credentials_for_org  # noqa: PLC0415
    from app.domain.orgs import Role as _Role  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    if not yaaos_session:
        return auth_failure_response("unauthenticated")
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return auth_failure_response("unauthenticated")
        user_row = await identity_repo.get_user(s, session.user_id)
        emails = await identity_repo.list_emails_for_user(s, session.user_id)
        membership_rows = await orgs_repo.list_memberships_for_user(s, session.user_id)
        memberships_view = []
        for m in membership_rows:
            org = await orgs_repo.get_org(s, m.org_id)
            if org is None:
                continue
            broken: list[dict[str, str | None]] = []
            # Owners + Admins see broken integrations; Members get an empty list.
            if _Role(m.role).covers(_Role.ADMIN):
                broken_creds = await list_broken_credentials_for_org(s, org.id)
                broken = [
                    {
                        "provider": c.provider,
                        "last_refresh_failed_at": (
                            c.last_refresh_failed_at.isoformat() if c.last_refresh_failed_at else None
                        ),
                    }
                    for c in broken_creds
                ]
            memberships_view.append(
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
            "memberships": memberships_view,
        }
    )


@router.get("/providers", dependencies=[Depends(public_route)])
async def providers() -> dict[str, list[str]]:
    """List registered provider ids. The SPA renders one button per id on
    the login page; the test stub appears only when YAAOS_ENV=test."""
    return {"providers": list_providers()}


@router.get("/sso/discover", dependencies=[Depends(public_route)])
@limiter.limit(AUTH_LIMIT)
async def sso_discover(request: Request, email: str) -> dict[str, Any]:
    """find the SSO IdP (if any) matching the email's domain.

    Drives the Login page's provider-button rendering per E2a.18:
    `{provider: "github" | "saml", saml_org_slug?: str}`.

    Looks up `sso_configs.email_domains` (JSONB array) for a row whose
    `enabled = true` claims the address's domain. First match wins
    (domain claims must be unique in practice; Owner-level admin UI is
    the gate). No SAML match → `{provider: "github"}` so existing
    GitHub-only orgs keep working.

    `email` is required but only the format is validated — we never
    confirm or deny that a given address belongs to a user.
    """
    del request  # rate-limit-only; FastAPI requires the kwarg
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail={"error": "invalid_email"})
    domain = email.split("@", 1)[1].strip().lower()
    if not domain:
        raise HTTPException(status_code=422, detail={"error": "invalid_email"})

    from app.domain.orgs import find_saml_org_slug_for_domain  # noqa: PLC0415

    slug = await find_saml_org_slug_for_domain(domain)
    if slug is None:
        return {"provider": "github"}
    return {"provider": "saml", "saml_org_slug": slug}


# ── # TOTP enroll + verify ──────────────────────────────────


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
    from app.core.identity import totp as totp_lifecycle  # noqa: PLC0415

    if not yaaos_session:
        return auth_failure_response("unauthenticated")
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return auth_failure_response("unauthenticated")
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
    from app.core.identity import totp as totp_lifecycle  # noqa: PLC0415

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
    raw_next = _safe_next(payload.get("next"))

    async with db_session() as s:
        ok = await totp_lifecycle.verify(s, user_id=user_id, code=body.code)
        if not ok:
            return JSONResponse(status_code=400, content={"error": "totp_invalid"})
        next_path = await _safe_next_for_user(s, raw_next, user_id=user_id)
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
    from app.core.identity import totp as totp_lifecycle  # noqa: PLC0415

    if not yaaos_session:
        return auth_failure_response("unauthenticated")
    async with db_session() as s:
        session = await session_lifecycle.lookup(s, yaaos_session)
        if session is None or session.user_id is None:
            return auth_failure_response("unauthenticated")
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


register_routes(RouteSpec(module_name="sessions", router=router, url_prefix="/api/auth"))


__all__ = ["router"]
