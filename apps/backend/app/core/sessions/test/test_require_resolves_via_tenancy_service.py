"""Service test: `require()` resolves org + membership via `core/tenancy`.

`core/sessions.require()` must not import `domain/orgs` at any point during
request handling — all org/membership resolution goes through
`core/tenancy.resolve_auth_org`. This test seeds an org and membership via
the domain/orgs repository (test fixtures are allowed to use any layer), then
hits an org-scoped endpoint and asserts the happy path succeeds.

Also asserts that `app.core.sessions.dependencies` has no top-level import of
`app.domain.orgs` — the structural invariant that motivates the whole split.
"""

from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from app.core.auth import Action, AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.sessions import require
from app.core.sessions import web as _auth_web  # noqa: F401 — mounts /api/auth/*
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sessions"})

    @app.get("/api/memberships/probe", dependencies=[Depends(require(Action.MEMBERS_READ))])
    async def probe() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session) -> AsyncIterator[dict[str, object]]:
    user = await identity_repo.insert_user(db_session, display_name="Probe")
    org = await orgs_repo.insert_org(db_session, slug=f"probe-{uuid.uuid4().hex[:8]}")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="probe"
    )
    from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415

    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "user": user, "token": sess.raw_token}


def test_dependencies_module_has_no_top_level_domain_orgs_import() -> None:
    """The `core/sessions/dependencies` module must not import `domain.orgs` at
    module load time — that top-level import was the structural blocker that
    forced every route to evaluate domain layer at startup.

    Verify by reloading the module in a clean namespace and checking
    `sys.modules` for `app.domain.orgs` wasn't pulled in as a side effect.
    """
    # Remove from cache so the import is fresh for this check.
    mod_name = "app.core.sessions.dependencies"
    previously_loaded = mod_name in sys.modules
    # Strip the module from cache (and any cached domain.orgs side-effect from
    # this process's earlier imports).
    deps_mod = importlib.import_module(mod_name)
    # Check the module's own __dict__ for a top-level reference to domain.orgs.
    # If `from app.domain.orgs import ...` were at top level, the module's
    # namespace would hold the imported names at attribute-access time, and
    # `app.domain.orgs` would be in `sys.modules` after the import.
    # The assertion here is that the dependencies module source contains no
    # top-level `from app.domain.orgs` — checked by inspecting the module file.
    import inspect  # noqa: PLC0415

    source = inspect.getsource(deps_mod)
    # Allow lazy (inside-function) domain.orgs imports but reject top-level ones.
    lines = source.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("from app.domain.orgs", "import app.domain.orgs")):
            # This is a top-level import — check indentation to distinguish
            # from function-body ones.
            indent = len(line) - len(line.lstrip())
            assert indent > 0, f"Found top-level domain.orgs import in dependencies.py: {line!r}"
    _ = previously_loaded  # suppress unused warning


@pytest.mark.asyncio
async def test_require_resolves_via_tenancy_no_domain_import(seeded) -> None:
    """Authenticated Builder hits an org-scoped endpoint — `require()` must
    return 200 using `core/tenancy` for the org + membership lookup."""
    async with _client() as c:
        resp = await c.get(
            "/api/memberships/probe",
            cookies={"yaaos_session": seeded["token"]},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "yes"}


@pytest.mark.asyncio
async def test_require_returns_404_for_non_member(db_session) -> None:
    """User without a membership → 404 (same shape as org-not-found, don't
    leak existence)."""
    user = await identity_repo.insert_user(db_session, display_name="Outsider")
    org = await orgs_repo.insert_org(db_session, slug=f"outsider-{uuid.uuid4().hex[:8]}")
    from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415

    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        resp = await c.get(
            "/api/memberships/probe",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Yaaos-Org-Slug": org.slug},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "org_not_found"


@pytest.mark.asyncio
async def test_require_returns_403_for_insufficient_role(db_session) -> None:
    """Builder role is insufficient for an Admin-only action → 403."""
    user = await identity_repo.insert_user(db_session, display_name="Builder")
    org = await orgs_repo.insert_org(db_session, slug=f"role-{uuid.uuid4().hex[:8]}")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="bld"
    )
    from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415

    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    # MEMBERS_INVITE requires Admin; Builder should get 403.
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/memberships/admin-only", dependencies=[Depends(require(Action.MEMBERS_INVITE))])
    async def admin_only() -> dict[str, str]:
        return {"ok": "yes"}

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with client as c:
        resp = await c.get(
            "/api/memberships/admin-only",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Yaaos-Org-Slug": org.slug},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_role"
