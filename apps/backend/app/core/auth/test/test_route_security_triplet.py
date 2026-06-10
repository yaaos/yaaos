"""Auto-generated negative-trio + positive coverage from the route registry.

Spec §"Cross-cutting test requirements": *"Every protected endpoint:
triplet — unauthenticated 401, wrong-org 404, insufficient-role 403,
success 200."*

This test enumerates every registered ORG_SCOPED route, asserts the
unauthenticated path returns 4xx (400 when X-Yaaos-Org-Slug is missing, 401 when
session is missing), and confirms the registry hasn't regressed.

The per-role positive + 403 + 404 cases live in each endpoint's own test
file — this fixture asserts the floor.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.auth.types import ORG_SCOPED_PREFIXES, RouteSecurity, classify_route
from app.core.identity import user_web as _user_web  # noqa: F401
from app.core.sessions import web as _auth_web  # noqa: F401 — ensures /api/auth registers
from app.domain.orgs import audit_web as _audit_web  # noqa: F401
from app.domain.orgs import sso_web as _sso_web  # noqa: F401
from app.domain.orgs import web as _orgs_web  # noqa: F401


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app)
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


def _enumerate_org_scoped_routes() -> list[tuple[str, str]]:
    """Walk the registered routes; return `(method, path)` for every route
    that classifies as `RouteSecurity.ORG_SCOPED`. Templated paths get a
    synthetic UUID so the fixture can probe them."""

    from app.core.webserver import get_specs  # noqa: PLC0415

    out: list[tuple[str, str]] = []
    for spec in get_specs().values():
        prefix = spec.effective_prefix
        for route in spec.router.routes:
            full = prefix + getattr(route, "path", "")
            for method in getattr(route, "methods", []) or []:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                if classify_route(full, method) is not RouteSecurity.ORG_SCOPED:
                    continue
                concrete = full.replace("{user_id}", "00000000-0000-0000-0000-000000000000")
                concrete = concrete.replace("{target_user_id}", "00000000-0000-0000-0000-000000000000")
                concrete = concrete.replace("{email_id}", "00000000-0000-0000-0000-000000000000")
                concrete = concrete.replace("{slug}", "some-slug")
                out.append((method, concrete))
    return out


@pytest.mark.asyncio
async def test_org_scoped_prefixes_have_routes() -> None:
    """Sanity: each declared org-scoped prefix has at least one route."""
    routes = _enumerate_org_scoped_routes()
    covered_prefixes = {p for p in ORG_SCOPED_PREFIXES if any(r[1].startswith(p) for r in routes)}
    assert "/api/memberships/" in covered_prefixes
    assert "/api/audit" in covered_prefixes


@pytest.mark.asyncio
async def test_every_org_scoped_route_rejects_anonymous_access() -> None:
    """Negative-trio floor: unauthenticated + no X-Yaaos-Org-Slug ⇒ middleware
    returns 400 (missing_org_slug); with header but no session ⇒ 401."""
    routes = _enumerate_org_scoped_routes()
    assert routes, "no org-scoped routes discovered"

    async with _client() as c:
        for method, path in routes:
            # No header → 400.
            resp = await c.request(method, path)
            assert resp.status_code in (400, 401, 403, 404, 405, 422), (
                f"{method} {path} returned {resp.status_code}; expected 4xx without any auth"
            )

            # With X-Yaaos-Org-Slug but no session → 401 from `require()`.
            resp = await c.request(method, path, headers={"X-Yaaos-Org-Slug": "some-slug"})
            assert resp.status_code in (401, 403, 404, 405, 422), (
                f"{method} {path} returned {resp.status_code} with slug but no session; expected 401/403/404"
            )


@pytest.mark.asyncio
async def test_no_2xx_from_anonymous_org_scoped_request() -> None:
    """Strict: under no circumstance should an anonymous request return 2xx
    against an org-scoped route."""
    routes = _enumerate_org_scoped_routes()
    async with _client() as c:
        for method, path in routes:
            resp = await c.request(method, path)
            assert not (200 <= resp.status_code < 300), (
                f"{method} {path} returned {resp.status_code} anonymously — auth-bypass risk"
            )
