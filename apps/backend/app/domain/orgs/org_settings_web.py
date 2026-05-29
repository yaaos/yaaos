"""HTTP wiring for top-level org settings + user-scoped/readiness endpoints.

| Method | Path                       | Action / auth                                       |
|--------|----------------------------|-----------------------------------------------------|
| GET    | `/api/orgs`                | `ORG_SETTINGS_READ` — top-level settings for the current org. |
| PATCH  | `/api/orgs`                | `ORG_SETTINGS_WRITE` — Owner/Admin update settings. |
| GET    | `/api/orgs/mine`           | session cookie only (cross-org) — picker + switcher. |
| GET    | `/api/orgs/config-status`  | `ORG_READ` — "not configured" gate aggregation. |

Org identified by `X-Org-Slug` header (RouteSecurity.ORG_SCOPED). Architecture.md documents
the URL as `/api/orgs/{slug}` for readability; this implementation mirrors the
other endpoints which all take the slug via header. The single endpoint
returns the updated org's relevant settings.

`workspace_provider` is `in_memory` or `remote_agent`. When set to
`remote_agent`, `registered_iam_arn` must also be set — the identity-exchange
verifier matches the agent's signed STS payload against this ARN.

`/api/orgs/mine` lives on the public allowlist (see `core/auth/types.py`)
because the SPA hits it before any org is selected — the session cookie
identifies the user; no `X-Org-Slug` header is involved. `last_used_at` is
null — there is no per-membership "last visited" column today
(Open Question 3 in ).
"""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Cookie, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import Action, Role, org_id_var, public_route
from app.core.database import session as db_session
from app.core.identity import repository as identity_repo
from app.core.sessions import require
from app.core.tenancy import (
    get_org_full as _get_org_full,
)
from app.core.tenancy import (
    list_memberships_for_org as _list_memberships_for_org,
)
from app.core.tenancy import (
    list_memberships_for_user as _list_memberships_for_user,
)
from app.core.tenancy import (
    update_org_fields as _update_org_fields,
)
from app.core.webserver import RouteSpec, register_routes
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.onboarding import get_onboarding_status

log = structlog.get_logger("orgs.settings.web")

router = APIRouter()


class _PatchOrgRequest(BaseModel):
    # Pydantic v2 allows the field to be absent OR explicitly null. Absent
    # = "don't touch"; null = "clear the override and fall back to the global
    # constant"; positive int = "set to N minutes".
    session_timeout_override: int | None = Field(default=None)
    _set_session_timeout_override: bool = False  # internal: did the client include the key?


_ALLOWED_WORKSPACE_PROVIDERS = {"in_memory", "remote_agent"}

# Strict registration shape for `registered_iam_arn`: partition `aws`, service
# `iam`, 12-digit account, `role/<name>` with NO path slashes. Full-matched
# because looser matching enables a cross-org escalation: STS returns
# `assumed-role/<name>/<session>` without echoing the IAM path, so a customer
# who registers `arn:aws:iam::ACCT:role/team/sub/foo` canonicalizes to
# `arn:aws:iam::ACCT:role/sub/foo` (wrong) and a *different* customer's role
# `arn:aws:iam::ACCT:role/foo` could end up matching the same canonical row.
# `[\w+=,.@-]+` matches AWS's documented IAM name character set.
_IAM_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@-]+$", re.ASCII)


class _OrgSettingsResponse(BaseModel):
    slug: str
    session_timeout_override: int | None
    workspace_provider: str | None = None
    registered_iam_arn: str | None = None
    aws_region: str | None = None


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


class _CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    slug: str = Field(min_length=1, max_length=64)


class _CreateOrgResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str


@router.post("", dependencies=[Depends(public_route)])
async def create_org(
    body: _CreateOrgRequest,
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> JSONResponse:
    """create an org from the picker page (E2a.19).

    The caller becomes Admin of the new org. Slug must be lowercase
    a-z / 0-9 / hyphens and unique. Returns 409 `slug_taken` if the slug
    is in use; 422 `invalid_slug` on bad characters; 401 on missing
    session.
    """
    if not yaaos_session:
        return JSONResponse(status_code=401, content={"error": "unauthenticated"})
    token_hash = identity_repo.hash_token(yaaos_session)
    async with db_session() as s:
        sess_row = await identity_repo.get_session_by_hash(s, token_hash)
        if sess_row is None or sess_row.user_id is None:
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        from datetime import UTC, datetime  # noqa: PLC0415

        if sess_row.expires_at < datetime.now(UTC):
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})

        slug = body.slug.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}[a-z0-9]|[a-z0-9]", slug):
            return JSONResponse(status_code=422, content={"error": "invalid_slug"})

        existing = await orgs_repo.get_org_by_slug(s, slug)
        if existing is not None:
            return JSONResponse(status_code=409, content={"error": "slug_taken"})

        org = await orgs_repo.insert_org(s, slug=slug, display_name=body.name.strip())
        await orgs_repo.insert_membership(
            s,
            user_id=sess_row.user_id,
            org_id=org.org_id,
            role=Role.ADMIN,
            handle=body.name.strip()[:64] or slug,
        )
        await s.commit()
    return JSONResponse(
        content=_CreateOrgResponse(
            id=org.org_id, slug=org.slug, name=org.display_name, role="admin"
        ).model_dump(mode="json")
    )


@router.get("", dependencies=[Depends(require(Action.ORG_SETTINGS_READ))])
async def get_org_settings() -> _OrgSettingsResponse:
    """Return the current org's top-level settings. Lets the SPA's Settings
    page show what's actually set before the user edits."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        full = await _get_org_full(s, org_id)
    if full is None:
        raise _err(404, "org_not_found")
    return _OrgSettingsResponse(
        slug=full.slug,
        session_timeout_override=full.session_timeout_override,
        workspace_provider=full.workspace_provider,
        registered_iam_arn=full.registered_iam_arn,
        aws_region=full.aws_region,
    )


@router.patch("", dependencies=[Depends(require(Action.ORG_SETTINGS_WRITE))])
async def patch_org_settings(body: dict) -> _OrgSettingsResponse:
    """Update top-level org settings. Body is a JSON object; only the keys
    actually present are touched. supports `session_timeout_override`
    (null clears it, positive int sets minutes)."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")

    async with db_session() as s:
        full = await _get_org_full(s, org_id)
        if full is None:
            raise _err(404, "org_not_found")

        # Resolve each settable field to its effective post-update value:
        # present in the body → the (validated) value; absent → the org's
        # current value, left unchanged.
        eff_timeout = full.session_timeout_override
        if "session_timeout_override" in body:
            value = body["session_timeout_override"]
            if value is not None:
                if not isinstance(value, int) or value <= 0:
                    raise _err(422, "invalid_session_timeout_override")
            eff_timeout = value

        eff_provider = full.workspace_provider
        if "workspace_provider" in body:
            value = body["workspace_provider"]
            if value is not None and value not in _ALLOWED_WORKSPACE_PROVIDERS:
                raise _err(422, "invalid_workspace_provider")
            eff_provider = value

        eff_arn = full.registered_iam_arn
        if "registered_iam_arn" in body:
            value = body["registered_iam_arn"]
            if value is not None:
                if not isinstance(value, str):
                    raise _err(422, "invalid_registered_iam_arn")
                # Lowercase before validation + storage. IAM names are
                # unique-case-insensitive in AWS, and `canonicalize_arn` in
                # `core/agent_gateway/sts_verifier` lowercases the STS-returned
                # ARN before lookup — both sides must agree.
                value = value.strip().lower()
                if not _IAM_ROLE_ARN_RE.fullmatch(value):
                    raise _err(422, "invalid_registered_iam_arn")
            eff_arn = value

        eff_region = full.aws_region
        if "aws_region" in body:
            value = body["aws_region"]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise _err(422, "invalid_aws_region")
            eff_region = value

        # Cross-field: `registered_iam_arn` and `aws_region` are both-or-neither
        # (matches the DB check constraint `ck_orgs_arn_region_paired`). Fail at
        # the application layer so the API returns a 422, not a 500 from the DB.
        if (eff_arn is None) != (eff_region is None):
            raise _err(422, "arn_and_region_must_be_paired")
        # Cross-field: remote_agent provider requires an ARN.
        if eff_provider == "remote_agent" and not eff_arn:
            raise _err(422, "remote_agent_requires_iam_arn")

        updated = await _update_org_fields(
            s,
            org_id,
            session_timeout_override=eff_timeout,
            workspace_provider=eff_provider,
            registered_iam_arn=eff_arn,
            aws_region=eff_region,
        )
        await s.commit()
    return _OrgSettingsResponse(
        slug=updated.slug,
        session_timeout_override=updated.session_timeout_override,
        workspace_provider=updated.workspace_provider,
        registered_iam_arn=updated.registered_iam_arn,
        aws_region=updated.aws_region,
    )


class MineOrgView(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str
    last_used_at: str | None


class ConfigStatusAdmin(BaseModel):
    user_id: UUID
    display_name: str
    primary_email: str | None


class ConfigStatusResponse(BaseModel):
    configured: bool
    missing: list[str]
    admins: list[ConfigStatusAdmin]


@router.get("/mine", dependencies=[Depends(public_route)])
async def list_mine(
    yaaos_session: Annotated[str | None, Cookie()] = None,
) -> JSONResponse:
    """Cross-org list of the user's memberships. Powers the org switcher and `/orgs` picker."""
    if not yaaos_session:
        return JSONResponse(status_code=401, content={"error": "unauthenticated"})
    token_hash = identity_repo.hash_token(yaaos_session)
    async with db_session() as s:
        row = await identity_repo.get_session_by_hash(s, token_hash)
        if row is None or row.user_id is None:
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        from datetime import UTC, datetime  # noqa: PLC0415

        if row.expires_at < datetime.now(UTC):
            return JSONResponse(status_code=401, content={"error": "unauthenticated"})
        memberships = await _list_memberships_for_user(s, row.user_id)
        out: list[MineOrgView] = []
        for m in memberships:
            out.append(
                MineOrgView(
                    id=m.org_id,
                    slug=m.slug,
                    name=m.org_name,
                    role=m.role.value,
                    last_used_at=None,
                )
            )
        out.sort(key=lambda o: o.slug)
    return JSONResponse(content=[o.model_dump(mode="json") for o in out])


@router.get("/config-status", dependencies=[Depends(require(Action.ORG_READ))])
async def config_status() -> ConfigStatusResponse:
    """Aggregated readiness for the "not configured" gate."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")

    status = await get_onboarding_status(org_id=org_id)

    missing: list[str] = []
    if not status.github_app_installed:
        missing.append("vcs")
    if not status.anthropic_key_set:
        missing.append("api_key")
    # Coding-agent readiness piggybacks on the BYOK contributor today —
    # `anthropic_key_set` implies a Claude Code plugin row was provisioned at
    # the same point in the onboarding flow. When more coding-agent plugins
    # ship (Codex, Aider), this collapses into a separate contributor.

    async with db_session() as s:
        org_full = await _get_org_full(s, org_id)
        if org_full is None:
            raise _err(404, "org_not_found")
        if not org_full.workspace_provider:
            missing.append("workspace_provider")

        admin_memberships = await _list_memberships_for_org(s, org_id)
        admins: list[ConfigStatusAdmin] = []
        for m in admin_memberships:
            if m.role.value not in ("owner", "admin"):
                continue
            user = await identity_repo.get_user(s, m.user_id)
            if user is None:
                continue
            emails = await identity_repo.list_emails_for_user(s, m.user_id)
            primary = next(
                (e.email for e in emails if e.is_primary),
                emails[0].email if emails else None,
            )
            admins.append(
                ConfigStatusAdmin(
                    user_id=m.user_id,
                    display_name=user.display_name,
                    primary_email=primary,
                )
            )

    return ConfigStatusResponse(
        configured=not missing,
        missing=missing,
        admins=admins,
    )


register_routes(RouteSpec(module_name="orgs", router=router, url_prefix="/api/orgs"))
