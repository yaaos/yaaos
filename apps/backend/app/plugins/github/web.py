"""HTTP routes owned by the github plugin: webhook receiver + plugin-owned settings/health.

Per `plan/milestones/M01-code-review/backend.md` § 2026-05-16, plugin-owned data
(install state, health) is served under the plugin's own `/api/github/...`
namespace — not aggregated under `/api/settings/`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

import httpx
import structlog
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.auth import public_route
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.plugins.github.models import GitHubAppInstallationRow, GitHubSettingsRow
from app.plugins.github.payload_parser import parse_webhook
from app.plugins.github.service import (
    mark_installation_inactive,
    mark_webhook_processed,
    record_webhook_event,
    run_catchup_loop,
    set_github_credentials,
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

    async with db_session() as s:
        settings_row = (await s.execute(select(GitHubSettingsRow).limit(1))).scalar_one_or_none()
    if settings_row is None:
        log.warning("github.webhook.no_settings_row")
        return JSONResponse(status_code=400, content={"error": "github_settings missing"})

    fernet = Fernet(get_settings().yaaos_encryption_key.encode())
    secret = fernet.decrypt(settings_row.encrypted_webhook_secret)
    if not verify_webhook_signature(body, x_hub_signature_256, secret):
        log.warning("github.webhook.bad_signature", delivery=x_github_delivery)
        return JSONResponse(status_code=400, content={"error": "bad signature"})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "bad json"})

    install_id = (payload.get("installation") or {}).get("id")
    org_id = settings_row.org_id
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


# ─── Credentials entry ───────────────────────────────────────────────────────


class SetCredentialsRequest(BaseModel):
    app_id: str = Field(..., min_length=1)
    slug: str = Field(..., min_length=1)
    private_key: str = Field(..., min_length=1)
    webhook_secret: str = Field(..., min_length=1)


@router.post("/credentials")
async def set_credentials(req: SetCredentialsRequest) -> dict[str, str]:
    """Encrypt + upsert the App's credentials. Operator pastes values from the
    GitHub App registration page (App ID, slug, private key PEM, webhook secret).
    """
    pem = req.private_key.strip()
    if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
        raise HTTPException(
            status_code=400,
            detail={"private_key": "must be a PEM-formatted private key (-----BEGIN ... PRIVATE KEY-----)"},
        )
    await set_github_credentials(
        M01_ORG_ID,
        app_id=req.app_id.strip(),
        slug=req.slug.strip(),
        private_key=pem,
        webhook_secret=req.webhook_secret.strip(),
    )
    return {"status": "saved"}


# ─── Manifest-flow callback ──────────────────────────────────────────────────


@router.get("/manifest-callback")
async def manifest_callback(
    code: str = Query(..., min_length=1),
) -> RedirectResponse:
    """Exchange the temporary `code` GitHub hands us after the operator creates
    yaaos's App via the manifest flow. Stores App ID / slug / PEM / webhook
    secret in `github_settings`, then redirects the browser back to /settings.

    The manifest flow is described at
    https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
    """
    base_url = get_settings().github_api_base_url
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/app-manifests/{code}/conversions",
                headers={"Accept": "application/vnd.github+json"},
            )
    except httpx.HTTPError as e:
        return RedirectResponse(
            url=f"/settings?gh_manifest_error={quote(f'could not reach GitHub: {e}')}",
            status_code=303,
        )
    if resp.status_code != 201:
        excerpt = resp.text[:200]
        return RedirectResponse(
            url=f"/settings?gh_manifest_error={quote(f'GitHub returned {resp.status_code}: {excerpt}')}",
            status_code=303,
        )
    data = resp.json()
    try:
        slug = data["slug"]
        await set_github_credentials(
            M01_ORG_ID,
            app_id=str(data["id"]),
            slug=slug,
            private_key=data["pem"],
            webhook_secret=data["webhook_secret"],
        )
    except KeyError as e:
        return RedirectResponse(
            url=f"/settings?gh_manifest_error={quote(f'GitHub response missing field {e}')}",
            status_code=303,
        )
    # Chain straight into the install flow: GitHub created the App, now the
    # operator picks the account + repos. The App's setup_url (set in the
    # manifest to <yaaos>/settings) brings them back here after install.
    return RedirectResponse(url=f"https://github.com/apps/{slug}/installations/new", status_code=303)


# ─── Installation state for the UI ───────────────────────────────────────────


class InstallationResponse(BaseModel):
    credentials_configured: bool
    installed: bool
    app_id: str | None = None
    slug: str | None = None
    account_login: str | None = None
    install_external_id: str | None = None
    installed_at: datetime | None = None
    install_url: str | None = None
    installations_url: str | None = None


@router.get("/installation")
async def installation() -> InstallationResponse:
    """Three-state response driving the Settings UI:
    1. credentials_configured=False → no App credentials yet; UI shows the creds form.
    2. credentials_configured=True, installed=False → show "Install on a repo" button.
    3. installed=True → show "Manage on GitHub" + account info.
    """
    async with db_session() as s:
        settings_row = (
            await s.execute(select(GitHubSettingsRow).where(GitHubSettingsRow.org_id == M01_ORG_ID))
        ).scalar_one_or_none()
        install_row = (
            await s.execute(
                select(GitHubAppInstallationRow)
                .where(
                    GitHubAppInstallationRow.org_id == M01_ORG_ID,
                    GitHubAppInstallationRow.status == "active",
                )
                .order_by(GitHubAppInstallationRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if settings_row is None:
        return InstallationResponse(credentials_configured=False, installed=False)

    slug = settings_row.slug
    install_url = f"https://github.com/apps/{slug}/installations/new" if slug else None

    if install_row is None:
        return InstallationResponse(
            credentials_configured=True,
            installed=False,
            app_id=settings_row.app_id,
            slug=slug or None,
            install_url=install_url,
        )

    return InstallationResponse(
        credentials_configured=True,
        installed=True,
        app_id=settings_row.app_id,
        slug=slug or None,
        account_login=install_row.account_login,
        install_external_id=install_row.install_external_id,
        installed_at=install_row.created_at,
        install_url=install_url,
        installations_url=f"https://github.com/settings/installations/{install_row.install_external_id}",
    )


@router.get("/repositories")
async def repositories() -> dict[str, object]:
    """Live list of repos the App can see, fetched from GitHub via the
    installation token. No yaaos-side allowlist — GitHub's install picker IS
    the authority. Used by the Settings GitHub card's "Repositories" section.

    Returns `{repositories: [{full_name, html_url, private}], total_count}`
    when the App is installed; an empty list when it isn't.
    """
    from app.domain import vcs as vcs_mod  # noqa: PLC0415
    from app.plugins.github.service import get_plugin as get_github_plugin  # noqa: PLC0415

    try:
        token = await vcs_mod.get_installation_token("github", M01_ORG_ID)
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
    async with db_session() as s:
        install = (
            await s.execute(
                select(GitHubAppInstallationRow).where(
                    GitHubAppInstallationRow.org_id == M01_ORG_ID,
                    GitHubAppInstallationRow.status == "active",
                )
            )
        ).scalar_one_or_none()
        settings_row = (
            await s.execute(select(GitHubSettingsRow).where(GitHubSettingsRow.org_id == M01_ORG_ID))
        ).scalar_one_or_none()

    now = datetime.now(UTC)
    if settings_row is None:
        return {
            "healthy": False,
            "message": "credentials not configured (App ID, private key, webhook secret, slug)",
            "checked_at": now,
        }
    if install is None:
        return {"healthy": False, "message": "GitHub App not installed on any repo", "checked_at": now}
    return {"healthy": True, "message": "ok", "checked_at": now}


async def _start_catchup() -> None:
    """Spawn the catch-up poller as a background task. We do NOT await it from
    the startup hook — the hook must return promptly so FastAPI can finish
    initializing. The poller sleeps for `yaaos_catchup_delay_seconds` first
    and then refreshes open-PR metadata across each install's visible repos.
    """
    from app.core.observability import spawn  # noqa: PLC0415

    spawn("github.catchup", run_catchup_loop())


# ── M02 — GitHub App install ↔ org binding ──────────────────────────────


_INSTALL_STATE_SALT = "yaaos-github-install"


def _install_state_serializer():
    from itsdangerous import URLSafeTimedSerializer  # noqa: PLC0415

    from app.core.config import get_settings  # noqa: PLC0415

    return URLSafeTimedSerializer(get_settings().yaaos_oauth_state_secret, salt=_INSTALL_STATE_SALT)


@router.get("/install")
async def github_install_start(request: Request) -> RedirectResponse:
    """Owner-initiated GitHub App install. Signs `state=<org_id>` via
    itsdangerous and 302's to the App install URL. Callback verifies the
    state and writes a `github_installations(org_id, installation_id)` row.

    Reads `org_id` from the `X-Org-Slug` header — the route is gated on
    the `GITHUB_APP_LINK` action (Owner) via the standard
    middleware/dep pair. Hitting this from `/orgs/<slug>/settings` brings
    the Owner through the install picker on GitHub.
    """
    from fastapi import HTTPException as _HTTPException  # noqa: PLC0415

    from app.core.auth import org_id_var  # noqa: PLC0415

    org_id = org_id_var.get()
    if org_id is None:
        raise _HTTPException(status_code=400, detail={"error": "no_org_context"})
    state = _install_state_serializer().dumps({"org_id": str(org_id)})
    # Build the install URL. The App's slug comes from settings (stored at
    # manifest-create time). If not yet provisioned, send the operator to
    # the manifest-create flow instead.
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.plugins.github.models import GitHubSettingsRow  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(_select(GitHubSettingsRow).where(GitHubSettingsRow.org_id == org_id))
        ).scalar_one_or_none()
    if row is None or not row.slug:
        raise _HTTPException(status_code=409, detail={"error": "app_not_provisioned"})
    redirect_to = f"https://github.com/apps/{row.slug}/installations/new?state={state}"
    from fastapi.responses import RedirectResponse as _RedirectResponse  # noqa: PLC0415

    return _RedirectResponse(redirect_to)


@router.get("/install_callback")
async def github_install_callback(request: Request) -> RedirectResponse:
    """Post-install redirect target. GitHub redirects here with
    `installation_id=<n>&state=<signed>` so we can bind the new install to
    the right org. On success, write the `github_installations` row and
    303 to /orgs/<slug>/settings.
    """
    from fastapi import HTTPException as _HTTPException  # noqa: PLC0415
    from fastapi.responses import RedirectResponse as _RedirectResponse  # noqa: PLC0415
    from itsdangerous import BadSignature, SignatureExpired  # noqa: PLC0415

    qp = request.query_params
    raw_state = qp.get("state")
    installation_id = qp.get("installation_id")
    if not raw_state or not installation_id:
        raise _HTTPException(status_code=400, detail={"error": "missing_params"})
    try:
        payload = _install_state_serializer().loads(raw_state, max_age=900)
    except SignatureExpired:
        raise _HTTPException(status_code=400, detail={"error": "state_expired"})
    except BadSignature:
        raise _HTTPException(status_code=400, detail={"error": "state_invalid"})

    from uuid import UUID as _UUID  # noqa: PLC0415

    try:
        org_id = _UUID(payload["org_id"])
    except (KeyError, ValueError):
        raise _HTTPException(status_code=400, detail={"error": "state_invalid"})

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.identity.models import GithubInstallationRow  # noqa: PLC0415

    async with db_session() as s:
        existing = (
            await s.execute(
                __import__("sqlalchemy")
                .select(GithubInstallationRow)
                .where(GithubInstallationRow.installation_id == int(installation_id))
            )
        ).scalar_one_or_none()
        first_bind = existing is None
        if existing is None:
            s.add(GithubInstallationRow(installation_id=int(installation_id), org_id=org_id))
        elif existing.org_id != org_id:
            existing.org_id = org_id
        if first_bind:
            from pydantic import BaseModel as _BaseModel  # noqa: PLC0415

            from app.core.audit_log import Actor as _Actor  # noqa: PLC0415
            from app.core.audit_log import audit as _audit  # noqa: PLC0415

            class _InstallAuditPayload(_BaseModel):
                installation_id: int

            await _audit(
                "github_installation",
                org_id,
                "github_app_installation_linked",
                _InstallAuditPayload(installation_id=int(installation_id)),
                _Actor(kind="system"),
                org_id=org_id,
                session=s,
            )

        # M03: register the github plugin as the org's VCS on first bind. The
        # picker UI delegates the install handshake to this endpoint; this is
        # where the org's VCS choice is durably recorded.
        if first_bind:
            from app.core.audit_log import Actor as _Actor2  # noqa: PLC0415
            from app.domain.orgs import set_vcs as _set_vcs  # noqa: PLC0415

            await _set_vcs(
                s,
                org_id=org_id,
                plugin_id="github",
                settings={"installation_id": int(installation_id)},
                actor=_Actor2(kind="system"),
            )
        await s.commit()

    return _RedirectResponse("/")


async def resolve_org_for_installation(installation_id: int):
    """Look up `(org_id)` by GitHub installation id via the M02
    `github_installations` table. Returns None if not bound yet."""
    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.identity.models import GithubInstallationRow  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(
                __import__("sqlalchemy")
                .select(GithubInstallationRow)
                .where(GithubInstallationRow.installation_id == int(installation_id))
            )
        ).scalar_one_or_none()
    return row.org_id if row else None


register_routes(RouteSpec(module_name="github", router=router, on_startup=[_start_catchup]))
