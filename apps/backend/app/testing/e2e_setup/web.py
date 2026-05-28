"""HTTP routes for the `e2e_setup` test surface.

Every route is gated on `is_non_prod` (dev + test). In prod the routes still mount
(because the testing tree is excluded from prod wheels, so prod never imports
this module — see `apps/backend/pyproject.toml`), but defense-in-depth here
ensures a stray dev-flagged build can't be probed for seed endpoints.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi import Depends as _Depends
from pydantic import BaseModel, Field

from app.core.auth import public_route as _public_route
from app.core.webserver import RouteSpec, register_routes
from app.testing.e2e_setup import service

router = APIRouter(dependencies=[_Depends(_public_route)])


def _guard_dev() -> None:
    if not service.is_dev_env():
        # 404 — pretend the route doesn't exist outside dev so prod scans
        # don't reveal the surface.
        raise HTTPException(status_code=404, detail="not found")


@router.post("/reset")
async def reset() -> dict[str, str]:
    _guard_dev()
    await service.reset()
    return {"status": "reset"}


class _GithubInstallRequest(BaseModel):
    org_login: str = Field(default="acme", min_length=1)
    target_org_slug: str | None = Field(default=None, min_length=1)


@router.post("/seed/github_install")
async def seed_github_install(
    req: _GithubInstallRequest | None = None,
) -> dict[str, str]:
    """Seed an active `github_app_installations` row + Claude Code settings.
    The platform GitHub App credentials come from env vars, so there's no
    per-org credential seed."""
    _guard_dev()
    payload = req or _GithubInstallRequest()
    await service.seed_github_install(
        org_login=payload.org_login,
        target_org_slug=payload.target_org_slug,
    )
    return {"status": "seeded"}


class _LessonRequest(BaseModel):
    repo_external_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


@router.post("/seed/lesson")
async def seed_lesson(req: _LessonRequest) -> dict[str, str]:
    _guard_dev()
    lesson_id: UUID = await service.seed_lesson(
        repo_external_id=req.repo_external_id,
        title=req.title,
        body=req.body,
    )
    return {"status": "seeded", "lesson_id": str(lesson_id)}


class _BrokenIntegrationRequest(BaseModel):
    org_slug: str = Field(..., min_length=1)
    provider: str = Field(default="linear", min_length=1)


@router.post("/seed/broken_integration")
async def seed_broken_integration(req: _BrokenIntegrationRequest) -> dict[str, str]:
    """Seed an `mcp_credentials` row with `last_refresh_status="failed"` so the
    broken-creds banner + Integrations settings badge surface in e2e specs."""
    _guard_dev()
    await service.seed_broken_integration(org_slug=req.org_slug, provider=req.provider)
    return {"status": "seeded"}


# ── auth-flow helpers ──────────────────────────────────────────────


class _BootstrapOwnerRequest(BaseModel):
    email: str = Field(..., min_length=3)
    github_id: str = Field(..., min_length=1)
    org_slug: str = Field(..., min_length=1)
    display_name: str = Field(default="Owner")
    provider: str = Field(default="github")


@router.post("/seed/bootstrap_owner")
async def seed_bootstrap_owner(req: _BootstrapOwnerRequest) -> dict[str, str]:
    """Mint a first user + org + Owner membership for an e2e test."""
    _guard_dev()
    ids = await service.seed_bootstrap_owner(
        email=req.email,
        github_id=req.github_id,
        org_slug=req.org_slug,
        display_name=req.display_name,
        provider=req.provider,
    )
    return {"status": "seeded", **ids}


class _UserWithSessionRequest(BaseModel):
    email: str = Field(..., min_length=3)
    session_cookie: str = Field(..., min_length=1)


@router.post("/seed/user_with_session")
async def seed_user_with_session(req: _UserWithSessionRequest) -> dict[str, str]:
    """Create a user + a session backed by `session_cookie` as the raw token."""
    _guard_dev()
    user_id = await service.seed_user_with_session(email=req.email, raw_session_token=req.session_cookie)
    return {"status": "seeded", "user_id": user_id}


class _StageProfileRequest(BaseModel):
    external_subject: str
    primary_email: str
    email_verified: bool = True
    display_name: str = ""


@router.post("/oauth_test/stage_profile")
async def oauth_test_stage_profile(req: _StageProfileRequest) -> dict[str, str]:
    """Stage the profile the `oauth_test` provider returns on next callback."""
    _guard_dev()
    service.stage_oauth_test_profile(
        external_subject=req.external_subject,
        primary_email=req.primary_email,
        email_verified=req.email_verified,
        display_name=req.display_name,
    )
    return {"status": "staged"}


@router.get("/email_inbox")
async def email_inbox() -> dict[str, list[dict[str, str]]]:
    """Return + clear the in-memory test-env email inbox."""
    _guard_dev()
    return {"messages": service.read_and_clear_email_inbox()}


class _SamlSignRequest(BaseModel):
    email: str
    name_id: str = ""


@router.post("/saml/sign")
async def saml_sign(req: _SamlSignRequest) -> dict[str, str]:
    """Test-only: sign a stub SAML assertion the `/api/sso/<slug>/acs`
    handler will accept. Drives the Phase 12 Playwright spec."""
    _guard_dev()
    from app.plugins.saml_test import sign_assertion  # noqa: PLC0415

    token = sign_assertion({"email": req.email, "name_id": req.name_id or req.email})
    return {"token": token}


register_routes(RouteSpec(module_name="e2e_setup", router=router, url_prefix="/api/testing"))
