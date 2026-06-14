"""HTTP routes owned by the github plugin: install state + health + the
install start/callback handshake. The GitHub webhook receiver lives at
`POST /api/intake/github` (see `domain/intake.web`); GitHub events flow
through the intake registry, not this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from sqlalchemy import select

from app.core.auth import Action, public_route
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.plugins.github.models import GitHubAppInstallationRow
from app.plugins.github.service import (
    fetch_install_account_login,
    upsert_installation,
)

log = structlog.get_logger("github.web")

DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# Default-deny: GitHub plugin routes declare `public_route`. The
# install/install_callback endpoints are SSO-style flows; settings endpoints
# go through `require(action)`.
router = APIRouter(dependencies=[Depends(public_route)])


# ─── Installation state for the UI ───────────────────────────────────────────


class InstallationResponse(BaseModel):
    app_configured: bool
    installed: bool
    slug: str | None = None
    account_login: str | None = None
    install_external_id: str | None = None
    installed_at: datetime | None = None
    installations_url: str | None = None


@router.get(
    "/installation",
    dependencies=[Depends(require(Action.VCS_READ))],
)
async def installation() -> InstallationResponse:
    """Two-state response driving the Settings UI:
    1. app_configured=False → platform GitHub App not provisioned (env vars blank).
    2. installed=False → show "Install yaaos on GitHub" button.
    3. installed=True → show "Manage on GitHub" + account info.
    """
    from app.core.auth import org_id_var  # noqa: PLC0415

    settings = get_settings()
    slug = settings.yaaos_github_app_slug
    # "App configured" tracks the GitHub *App* (install flow) only — the
    # OAuth App credentials power sign-in and are unrelated to whether the
    # install button should render.
    app_configured = bool(
        settings.yaaos_github_app_id and slug and settings.yaaos_github_app_private_key.get_secret_value()
    )
    if not app_configured:
        return InstallationResponse(app_configured=False, installed=False)

    org_id = org_id_var.get() or DEFAULT_ORG_ID
    async with db_session() as s:
        install_row = (
            await s.execute(
                select(GitHubAppInstallationRow)
                .where(
                    GitHubAppInstallationRow.org_id == org_id,
                    GitHubAppInstallationRow.status == "active",
                )
                .order_by(GitHubAppInstallationRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if install_row is None:
        return InstallationResponse(app_configured=True, installed=False, slug=slug)

    return InstallationResponse(
        app_configured=True,
        installed=True,
        slug=slug,
        account_login=install_row.account_login,
        install_external_id=install_row.install_external_id,
        installed_at=install_row.created_at,
        installations_url=f"{settings.github_web_base_url}/settings/installations/{install_row.install_external_id}",
    )


@router.get(
    "/repositories",
    dependencies=[Depends(require(Action.VCS_READ))],
)
async def repositories() -> dict[str, object]:
    """Live list of repos the App can see, fetched from GitHub via the
    installation token. No yaaos-side allowlist — GitHub's install picker IS
    the authority. Used by the Settings GitHub card's "Repositories" section.

    Returns `{repositories: [{full_name, html_url, private}], total_count}`
    when the App is installed; an empty list when it isn't.
    """
    from app.core import vcs as vcs_mod  # noqa: PLC0415
    from app.core.auth import org_id_var  # noqa: PLC0415
    from app.plugins.github.service import get_plugin as get_github_plugin  # noqa: PLC0415

    org_id = org_id_var.get() or DEFAULT_ORG_ID
    try:
        token = await vcs_mod.get_installation_token("github", org_id)
    except Exception as e:
        return {"repositories": [], "total_count": 0, "error": f"install token: {e}"}

    base_url = get_github_plugin().base_url
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base_url}/installation/repositories",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 100},
            )
    except httpx.HTTPError as e:
        return {"repositories": [], "total_count": 0, "error": f"github api unreachable: {e}"}
    if resp.status_code != 200:
        return {"repositories": [], "total_count": 0, "error": f"github api HTTP {resp.status_code}"}
    body = resp.json()
    repos = body.get("repositories", [])
    return {
        "total_count": body.get("total_count", len(repos)),
        "repositories": [
            {
                "full_name": r.get("full_name"),
                "html_url": r.get("html_url"),
                "private": r.get("private", False),
            }
            for r in repos
        ],
    }


@router.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    now = datetime.now(UTC)
    if not (settings.yaaos_github_app_id and settings.yaaos_github_app_private_key.get_secret_value()):
        return {
            "healthy": False,
            "message": "yaaos GitHub App not provisioned (env vars unset)",
            "checked_at": now,
        }
    async with db_session() as s:
        install = (
            await s.execute(
                select(GitHubAppInstallationRow).where(
                    GitHubAppInstallationRow.org_id == DEFAULT_ORG_ID,
                    GitHubAppInstallationRow.status == "active",
                )
            )
        ).scalar_one_or_none()
    if install is None:
        return {"healthy": False, "message": "GitHub App not installed on any repo", "checked_at": now}
    return {"healthy": True, "message": "ok", "checked_at": now}


# ── GitHub App install ↔ org binding ──────────────────────────────


_INSTALL_STATE_SALT = "yaaos-github-install"


def _install_state_serializer():
    from itsdangerous import URLSafeTimedSerializer  # noqa: PLC0415

    from app.core.config import get_settings  # noqa: PLC0415

    return URLSafeTimedSerializer(
        get_settings().yaaos_oauth_state_secret.get_secret_value(), salt=_INSTALL_STATE_SALT
    )


class InstallStartResponse(BaseModel):
    redirect_url: str


@router.post(
    "/install/start",
    dependencies=[Depends(require(Action.GITHUB_APP_LINK))],
)
async def github_install_start() -> InstallStartResponse:
    """Owner-initiated GitHub App install. Returns the App's install URL with
    a signed `state=<org_id>` query param. The SPA's button click POSTs here
    (so `X-Yaaos-Org-Slug` + `X-CSRF-Token` reach the auth chain) and then sets
    `window.location.href = redirect_url` to send the browser to GitHub.

    The callback at `/install_callback` verifies the signed state and writes
    a `github_app_installations` row.
    """
    from app.core.auth import org_id_var  # noqa: PLC0415

    settings = get_settings()
    slug = settings.yaaos_github_app_slug
    if not slug:
        raise HTTPException(status_code=409, detail={"error": "app_not_provisioned"})
    org_id = org_id_var.get()
    if org_id is None:
        raise HTTPException(status_code=400, detail={"error": "no_org_context"})
    state = _install_state_serializer().dumps({"org_id": str(org_id)})
    return InstallStartResponse(
        redirect_url=f"{settings.github_web_base_url}/apps/{slug}/installations/new?state={state}"
    )


@router.get("/install_callback")
async def github_install_callback(request: Request) -> RedirectResponse:
    """Post-install redirect target. GitHub redirects here with
    `installation_id=<n>&state=<signed>` so we can bind the new install to
    the right org.

    Writes the binding directly into `github_app_installations` (the single
    source of truth) with `account_login` fetched from GitHub's
    `GET /app/installations/{id}`. Going through the App API rather than
    waiting for the `installation.created` webhook means dev environments
    without a webhook tunnel still get a complete install row.
    """
    from itsdangerous import BadSignature, SignatureExpired  # noqa: PLC0415

    qp = request.query_params
    raw_state = qp.get("state")
    installation_id = qp.get("installation_id")
    if not raw_state or not installation_id:
        raise HTTPException(status_code=400, detail={"error": "missing_params"})
    try:
        payload = _install_state_serializer().loads(raw_state, max_age=900)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail={"error": "state_expired"})
    except BadSignature:
        raise HTTPException(status_code=400, detail={"error": "state_invalid"})

    try:
        org_id = UUID(payload["org_id"])
    except KeyError, ValueError:
        raise HTTPException(status_code=400, detail={"error": "state_invalid"})

    install_id_int = int(installation_id)

    # Fetch account_login so the row is immediately complete. Failures here
    # degrade to "" — the webhook will populate it shortly when it arrives.
    try:
        account_login = await fetch_install_account_login(install_id_int)
    except Exception as exc:
        # inside-span failure: FastAPI span is active; record on it
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, str(exc))
        log.exception("github.install_callback.fetch_account_failed")
        account_login = ""

    # Detect first-bind: only emit the audit + set_vcs side-effects when this
    # install row is genuinely new (not just an idempotent re-callback).
    async with db_session() as s:
        existing = (
            await s.execute(
                select(GitHubAppInstallationRow).where(
                    GitHubAppInstallationRow.install_external_id == str(install_id_int)
                )
            )
        ).scalar_one_or_none()
    first_bind = existing is None

    await upsert_installation(
        install_external_id=str(install_id_int),
        account_login=account_login,
        org_id=org_id,
    )

    if first_bind:
        from pydantic import BaseModel as _BaseModel  # noqa: PLC0415

        from app.core.audit_log import Actor as _Actor  # noqa: PLC0415
        from app.core.audit_log import audit as _audit  # noqa: PLC0415
        from app.domain.orgs import set_vcs as _set_vcs  # noqa: PLC0415

        class _InstallAuditPayload(_BaseModel):
            installation_id: int

        async with db_session() as s:
            await _audit(
                "github_installation",
                org_id,
                "github_app_installation_linked",
                _InstallAuditPayload(installation_id=install_id_int),
                _Actor(kind="system"),
                org_id=org_id,
                session=s,
            )
            await _set_vcs(
                s,
                org_id=org_id,
                plugin_id="github",
                settings={"installation_id": install_id_int},
                actor=_Actor(kind="system"),
            )
            await s.commit()

    return RedirectResponse("/")


register_routes(RouteSpec(module_name="github", router=router))
