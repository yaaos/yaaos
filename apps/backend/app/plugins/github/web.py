"""HTTP routes owned by the github plugin: webhook receiver + plugin-owned settings/health.

Per `plan/milestones/M01-code-review/backend.md` § 2026-05-16, plugin-owned data
(install state, health) is served under the plugin's own `/api/github/...`
namespace — not aggregated under `/api/settings/`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.core.auth import public_route
from app.core.auth.types import Action
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.domain.sessions.dependencies import require
from app.plugins.github.models import GitHubAppInstallationRow
from app.plugins.github.payload_parser import parse_webhook
from app.plugins.github.service import (
    fetch_install_account_login,
    mark_installation_inactive,
    mark_webhook_processed,
    record_webhook_event,
    upsert_installation,
    verify_webhook_signature,
)

log = structlog.get_logger("github.webhook")

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# M02 default-deny: GitHub plugin routes declare `public_route`. The webhook
# endpoint authenticates via HMAC signature; the install/install_callback
# endpoints are SSO-style flows. M03+ may swap install_* to `require()`.
router = APIRouter(dependencies=[Depends(public_route)])


# ─── Webhook receiver ────────────────────────────────────────────────────────


@router.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()

    secret = get_settings().yaaos_github_app_webhook_secret.get_secret_value()
    if not secret:
        log.warning("github.webhook.no_secret_configured")
        return JSONResponse(status_code=400, content={"error": "github app not configured"})
    if not verify_webhook_signature(body, x_hub_signature_256, secret.encode()):
        log.warning("github.webhook.bad_signature", delivery=x_github_delivery)
        return JSONResponse(status_code=400, content={"error": "bad signature"})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "bad json"})

    # Resolve org via the installation lookup. M01 single-org default applies
    # only when no install row matches the inbound delivery.
    install_id = (payload.get("installation") or {}).get("id")
    org_id = M01_ORG_ID
    if install_id is not None:
        async with db_session() as s:
            install = (
                await s.execute(
                    select(GitHubAppInstallationRow).where(
                        GitHubAppInstallationRow.install_external_id == str(install_id)
                    )
                )
            ).scalar_one_or_none()
        if install is not None:
            org_id = install.org_id

    row_id = await record_webhook_event(
        x_github_delivery or f"event-{id(payload)}",
        x_github_event,
        payload,
        org_id=org_id,
    )
    if row_id is None:
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # Install lifecycle events update `github_app_installations` directly —
    # these are infrastructure state, not VCS events for intake. Doing it here
    # keeps the plugin self-contained.
    if x_github_event == "installation" and install_id is not None:
        action = payload.get("action")
        account = (payload.get("installation") or {}).get("account") or {}
        account_login = account.get("login", "")
        if action in ("created", "new_permissions_accepted", "unsuspend"):
            await upsert_installation(
                install_external_id=str(install_id),
                account_login=account_login,
                org_id=org_id,
            )
        elif action == "deleted":
            await mark_installation_inactive(install_external_id=str(install_id), status="uninstalled")
        elif action == "suspend":
            await mark_installation_inactive(install_external_id=str(install_id), status="suspended")

    events = parse_webhook(x_github_event, x_github_delivery or str(row_id), payload)
    # Force-push enrichment: `pull_request.synchronize` events arrive with
    # `force_push=False`; the authoritative answer requires a `/compare` call
    # against GitHub. Do it here so the parsed event carries the true flag.
    if events and x_github_event == "pull_request" and payload.get("action") == "synchronize":
        from app.domain.vcs import PullRequestSynchronized  # noqa: PLC0415
        from app.plugins.github.service import get_plugin as _get_plugin  # noqa: PLC0415

        before_sha = payload.get("before") or ""
        after_sha = payload.get("after") or (payload.get("pull_request") or {}).get("head", {}).get("sha", "")
        repo_full = (payload.get("repository") or {}).get("full_name", "")
        try:
            is_force = await _get_plugin().detect_force_push(repo_full, before_sha, after_sha)
        except Exception:
            log.exception("github.webhook.force_push_detect_failed", delivery=x_github_delivery)
            is_force = False
        events = [
            e.model_copy(update={"force_push": is_force}) if isinstance(e, PullRequestSynchronized) else e
            for e in events
        ]

    if events:
        from app.domain.intake import handle_vcs_events  # noqa: PLC0415

        try:
            await handle_vcs_events(events, org_id=org_id)
        except Exception:
            log.exception("github.webhook.dispatch_failed", delivery=x_github_delivery)

    await mark_webhook_processed(row_id)
    return JSONResponse(status_code=200, content={"status": "ok"})


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

    org_id = org_id_var.get() or M01_ORG_ID
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
    from app.core.auth import org_id_var  # noqa: PLC0415
    from app.domain import vcs as vcs_mod  # noqa: PLC0415
    from app.plugins.github.service import get_plugin as get_github_plugin  # noqa: PLC0415

    org_id = org_id_var.get() or M01_ORG_ID
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
                    GitHubAppInstallationRow.org_id == M01_ORG_ID,
                    GitHubAppInstallationRow.status == "active",
                )
            )
        ).scalar_one_or_none()
    if install is None:
        return {"healthy": False, "message": "GitHub App not installed on any repo", "checked_at": now}
    return {"healthy": True, "message": "ok", "checked_at": now}


# ── M02 — GitHub App install ↔ org binding ──────────────────────────────


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
    (so `X-Org-Slug` + `X-CSRF-Token` reach the auth chain) and then sets
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
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail={"error": "state_invalid"})

    install_id_int = int(installation_id)

    # Fetch account_login so the row is immediately complete. Failures here
    # degrade to "" — the webhook will populate it shortly when it arrives.
    try:
        account_login = await fetch_install_account_login(install_id_int)
    except Exception:
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
