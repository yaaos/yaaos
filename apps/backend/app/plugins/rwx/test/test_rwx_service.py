"""Service tests for plugins/rwx — validator registration and api_keys endpoint integration.

Tests:
- GET /api/api-keys includes 'rwx' once bootstrap() registers the validator.
- POST /api/api-keys/rwx/validate dispatches to the registered validator callable.

The outbound HTTP probe in validate_rwx_token is stubbed via DI (register_validator
with a stub callable) so no network calls are made.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.core.api_keys
import app.core.sessions
from app.core.auth import AuthMiddleware, Role
from app.core.identity import create_user, mint_session
from app.domain.orgs import insert_membership, insert_org


def _app() -> FastAPI:
    """Minimal FastAPI app with auth middleware and api_keys routes only."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"api_keys"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.fixture(autouse=True)
def _ensure_rwx_registered() -> None:
    """Ensure the rwx validator is in the registry before each test."""
    from app.plugins.rwx import bootstrap  # noqa: PLC0415

    if app.core.api_keys.get_validator("rwx") is None:
        bootstrap()


@pytest_asyncio.fixture
async def seeded(db_session):
    """Seed user, org, membership, and a session cookie for API calls."""
    admin = await create_user(db_session, display_name="A")
    org = await insert_org(db_session, slug="rwx-test-org")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    return {"org": org, "admin": admin, "admin_sess": admin_sess}


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_providers_surfaces_rwx(seeded) -> None:
    """GET /api/api-keys includes 'rwx' after bootstrap() registers the validator."""
    async with _client() as client:
        resp = await client.get(
            "/api/api-keys",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 200
    providers = {p["provider"] for p in resp.json()}
    assert "rwx" in providers


@pytest.mark.asyncio
@pytest.mark.service
async def test_validate_rwx_dispatches_to_registered_validator(seeded, db_session) -> None:
    """POST /api/api-keys/rwx/validate calls the registered validator with the stored key.

    The real validate_rwx_token makes an outbound HTTP call; here we register a stub
    callable via register_validator (DI) so no network call occurs.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415

    # Seed an rwx key for the org.
    actor = Actor.user(user_id=seeded["admin"].id)
    await app.core.api_keys.set(
        seeded["org"].org_id, "rwx", "stub-rwx-token", actor=actor, session=db_session
    )
    await db_session.commit()

    # Register a stub validator via DI — no HTTP probe.
    captured: list[str] = []

    async def _stub_validator(plaintext: str) -> bool:
        captured.append(plaintext)
        return True

    app.core.api_keys.register_validator("rwx", _stub_validator)

    try:
        async with _client() as client:
            resp = await client.post(
                "/api/api-keys/rwx/validate",
                cookies={
                    "yaaos_session": seeded["admin_sess"].raw_token,
                    "yaaos_csrf": seeded["admin_sess"].csrf_token,
                },
                headers={
                    "X-Yaaos-Org-Slug": seeded["org"].slug,
                    "X-CSRF-Token": seeded["admin_sess"].csrf_token,
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"valid": True}
        assert captured == ["stub-rwx-token"]
    finally:
        # Restore the real rwx validator.
        from app.plugins.rwx.service import validate_rwx_token  # noqa: PLC0415

        app.core.api_keys.register_validator("rwx", validate_rwx_token)
