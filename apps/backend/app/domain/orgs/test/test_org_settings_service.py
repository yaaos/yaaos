"""Service-level coverage for PATCH /api/orgs workspace-settings behaviour.

Covers app-layer cross-org ARN collision check (returns 422, not 500 from
the DB unique constraint) and the removal of `workspace_provider` from the
patch body.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401
from app.core.tenancy import update_org_fields
from app.domain.orgs import org_settings_web as _org_settings_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import web as _orgs_web  # noqa: F401


def _patch_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"orgs"})
    return app


def _patch_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_patch_app()), base_url="http://test")


@pytest_asyncio.fixture
async def two_orgs(db_session):
    """Two orgs, each with an admin session. Used to verify cross-org ARN collision."""
    admin_a = await identity_repo.insert_user(db_session, display_name="Admin A")
    admin_b = await identity_repo.insert_user(db_session, display_name="Admin B")

    org_a = await orgs_repo.insert_org(db_session, slug="org-arn-a")
    org_b = await orgs_repo.insert_org(db_session, slug="org-arn-b")

    await orgs_repo.insert_membership(
        db_session, user_id=admin_a.id, org_id=org_a.org_id, role=Role.ADMIN, handle="adm-a"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin_b.id, org_id=org_b.org_id, role=Role.ADMIN, handle="adm-b"
    )

    sess_a = await session_lifecycle.create(db_session, user_id=admin_a.id, workspace_id=None)
    sess_b = await session_lifecycle.create(db_session, user_id=admin_b.id, workspace_id=None)
    await db_session.commit()

    yield {
        "org_a": org_a,
        "org_b": org_b,
        "sess_a": sess_a,
        "sess_b": sess_b,
    }


@pytest.mark.service
@pytest.mark.asyncio
async def test_duplicate_arn_across_orgs_returns_422(two_orgs) -> None:
    """Saving an IAM ARN already registered to another org returns 422, not
    a 500 from the DB unique constraint. The app-layer collision check fires
    before the write so the caller gets a clean error code.
    """
    arn = "arn:aws:iam::123456789012:role/shared-agent"
    region = "us-east-1"

    # Org A registers the ARN first.
    sess_a = two_orgs["sess_a"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"registered_iam_arn": arn, "aws_region": region},
            cookies={"yaaos_session": sess_a.raw_token, "yaaos_csrf": sess_a.csrf_token},
            headers={"X-Org-Slug": two_orgs["org_a"].slug, "X-CSRF-Token": sess_a.csrf_token},
        )
    assert r.status_code == 200, f"org_a first-save failed: {r.text}"

    # Org B tries to register the same ARN.
    sess_b = two_orgs["sess_b"]
    async with _patch_client() as c:
        r2 = await c.patch(
            "/api/orgs",
            json={"registered_iam_arn": arn, "aws_region": region},
            cookies={"yaaos_session": sess_b.raw_token, "yaaos_csrf": sess_b.csrf_token},
            headers={"X-Org-Slug": two_orgs["org_b"].slug, "X-CSRF-Token": sess_b.csrf_token},
        )
    assert r2.status_code == 422, f"expected 422 collision, got {r2.status_code}: {r2.text}"
    assert r2.json()["detail"]["error"] == "arn_already_registered"


@pytest.mark.service
@pytest.mark.asyncio
async def test_same_org_re_saving_arn_is_allowed(two_orgs, db_session) -> None:
    """An org can re-save its own ARN without a collision error — the check
    excludes the current org from the uniqueness scan."""
    arn = "arn:aws:iam::123456789012:role/my-agent"
    region = "us-east-2"

    # Seed org_a with the ARN directly so we bypass the HTTP layer for setup.
    await update_org_fields(
        db_session,
        two_orgs["org_a"].org_id,
        registered_iam_arn=arn,
        aws_region=region,
    )
    await db_session.commit()

    # Re-saving the same ARN via PATCH must succeed.
    sess_a = two_orgs["sess_a"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"registered_iam_arn": arn, "aws_region": region},
            cookies={"yaaos_session": sess_a.raw_token, "yaaos_csrf": sess_a.csrf_token},
            headers={"X-Org-Slug": two_orgs["org_a"].slug, "X-CSRF-Token": sess_a.csrf_token},
        )
    assert r.status_code == 200, f"re-save of own ARN failed: {r.text}"
