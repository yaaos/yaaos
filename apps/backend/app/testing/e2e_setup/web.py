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


class _RepoSkillRequest(BaseModel):
    org_slug: str = Field(..., min_length=1)
    repo_external_id: str = Field(..., min_length=1)
    skill_name: str = Field(..., min_length=1)


@router.post("/seed/repo_skill")
async def seed_repo_skill(req: _RepoSkillRequest) -> dict[str, str]:
    """Seed a ``skill_name`` for a connected repo so reviews can build an invocation."""
    _guard_dev()
    await service.seed_repo_skill(
        org_slug=req.org_slug,
        repo_external_id=req.repo_external_id,
        skill_name=req.skill_name,
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
    handler will accept. Drives the SSO Playwright spec."""
    _guard_dev()
    from app.plugins.saml_test import sign_assertion  # noqa: PLC0415

    token = sign_assertion({"email": req.email, "name_id": req.name_id or req.email})
    return {"token": token}


class _MemberForOrgRequest(BaseModel):
    org_slug: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)
    github_id: str = Field(..., min_length=1)
    role: str = Field(default="builder", pattern="^(owner|admin|builder)$")
    display_name: str = Field(default="Member")
    provider: str = Field(default="github")


@router.post("/seed/member_for_org")
async def seed_member_for_org(req: _MemberForOrgRequest) -> dict[str, str]:
    """Create a user + OAuth identity + org membership on an existing org.

    Used by e2e specs that need a non-owner role (e.g. builder-readonly tests).
    Returns ``{"user_id": ..., "org_id": ..., "org_slug": ...}``.
    """
    _guard_dev()
    ids = await service.seed_member_for_org(
        org_slug=req.org_slug,
        email=req.email,
        github_id=req.github_id,
        role=req.role,
        display_name=req.display_name,
        provider=req.provider,
    )
    return {"status": "seeded", **ids}


class _WorkspaceAgentRequest(BaseModel):
    org_slug: str = Field(..., min_length=1)
    lifecycle: str | None = Field(
        default=None,
        pattern="^(unconfigured|active|draining|shutdown)$",
    )


@router.post("/seed/workspace_agent")
async def seed_workspace_agent(req: _WorkspaceAgentRequest) -> dict[str, str]:
    """Seed a reachable workspace-agent row for the given org slug.

    Returns ``{"id": "<uuid>", "instance_id": "<string>"}`` so e2e specs can
    assert the card appears on the Workspaces page without knowing the PK in advance.
    An optional ``lifecycle`` field overrides the default ``"unconfigured"`` state.
    """
    _guard_dev()
    return await service.seed_workspace_agent(org_slug=req.org_slug, lifecycle=req.lifecycle)


class _DeregisterWorkspaceAgentRequest(BaseModel):
    id: UUID


@router.post("/seed/deregister_workspace_agent")
async def deregister_workspace_agent(req: _DeregisterWorkspaceAgentRequest) -> dict[str, str]:
    """Simulate an agent's graceful-shutdown signal for the given canonical id.

    Marks the agent offline + publishes ``agent_changed`` so the dashboard
    flips the card without a running container. Drives the graceful-shutdown
    Playwright spec.
    """
    _guard_dev()
    return await service.deregister_workspace_agent(agent_id=req.id)


class _SeedAgentRequest(BaseModel):
    org_id: UUID
    iam_arn: str = Field(default="arn:aws:iam::123456789012:role/yaaos-agent", min_length=1)
    version: str = Field(default="0.0.1", min_length=1)
    instance_id: str | None = Field(default=None, min_length=1)


@router.post("/seed/agent")
async def seed_agent(req: _SeedAgentRequest) -> dict[str, str]:
    """Insert a workspace-agent row and return ``{"id": "<uuid>", "instance_id": ...}``."""
    _guard_dev()
    result = await service.seed_agent(
        org_id=req.org_id,
        iam_arn=req.iam_arn,
        version=req.version,
        instance_id=req.instance_id,
    )
    return {"id": str(result["id"]), "instance_id": result["instance_id"]}


class _SeedWorkspaceRequest(BaseModel):
    org_id: UUID
    provider_id: str = Field(default="remote_agent", min_length=1)
    sha: str = Field(..., min_length=1)
    agent_id: UUID
    current_command_id: UUID | None = None
    status: str | None = None


@router.post("/seed/workspace")
async def seed_workspace(req: _SeedWorkspaceRequest) -> dict[str, str]:
    """Insert a workspace row and return ``{"workspace_id": "<uuid>"}``."""
    _guard_dev()
    ws_id = await service.seed_workspace(
        org_id=req.org_id,
        provider_id=req.provider_id,
        sha=req.sha,
        agent_id=req.agent_id,
        current_command_id=req.current_command_id,
        status=req.status,
    )
    return {"workspace_id": ws_id}


@router.delete("/orgs/{org_id}")
async def delete_org(org_id: UUID) -> dict[str, bool]:
    """Hard-delete an org row (cascades to all child rows)."""
    _guard_dev()
    await service.delete_org(org_id)
    return {"deleted": True}


@router.delete("/users/{user_id}/artifacts")
async def delete_user_artifacts(user_id: UUID) -> dict[str, bool]:
    """Hard-delete a user row and all identity-owned child rows."""
    _guard_dev()
    await service.delete_user(user_id)
    return {"deleted": True}


class _SetOrgIamArnRequest(BaseModel):
    org_slug: str
    iam_arn: str
    aws_region: str = "us-east-1"


@router.post("/seed/org_iam_arn")
async def set_org_iam_arn(req: _SetOrgIamArnRequest) -> dict[str, str]:
    """Override an org's IAM ARN to a custom value.

    Useful in tests that need a configured org (non-null ``registered_iam_arn``)
    without the test-agent Docker container registering to it — supply an ARN
    that mock-aws never returns.
    """
    _guard_dev()
    return await service.set_org_iam_arn(
        org_slug=req.org_slug, iam_arn=req.iam_arn, aws_region=req.aws_region
    )
