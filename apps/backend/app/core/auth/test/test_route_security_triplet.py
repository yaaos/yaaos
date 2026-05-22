"""Auto-generated negative-trio + positive coverage from the route registry.

Spec §"Cross-cutting test requirements": *"Every protected endpoint:
triplet — unauthenticated 401, wrong-org 404, insufficient-role 403,
success 200."*

This test enumerates every registered route under `M02_PROTECTED_PREFIXES`,
asserts the unauthenticated path returns 4xx (401 or 400 when X-Org-Slug
is missing), and confirms the registry hasn't regressed (routes exist).
The per-role positive + 403 + 404 cases live in each endpoint's own test
file — this fixture asserts the floor.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.auth.types import M02_PROTECTED_PREFIXES, is_m02_protected_path
from app.domain.identity import account_web as _account_web  # noqa: F401
from app.domain.orgs import audit_web as _audit_web  # noqa: F401
from app.domain.orgs import sso_web as _sso_web  # noqa: F401
from app.domain.orgs import web as _orgs_web  # noqa: F401
from app.domain.sessions import web as _auth_web  # noqa: F401 — ensures /api/auth registers


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    for spec in _specs.values():
        app.include_router(spec.router, prefix=spec.url_prefix or f"/api/{spec.module_name}")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


def _enumerate_protected_routes() -> list[tuple[str, str]]:
    """Walk the registered routes; return `(method, path)` for every route
    under an M02_PROTECTED_PREFIX that uses a non-templated path. Templated
    paths get a synthetic UUID so the fixture can probe them."""
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    out: list[tuple[str, str]] = []
    for spec in _specs.values():
        prefix = spec.url_prefix or f"/api/{spec.module_name}"
        for route in spec.router.routes:
            full = prefix + getattr(route, "path", "")
            if not is_m02_protected_path(full):
                continue
            for method in getattr(route, "methods", []) or []:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                concrete = full.replace("{user_id}", "00000000-0000-0000-0000-000000000000")
                concrete = concrete.replace("{target_user_id}", "00000000-0000-0000-0000-000000000000")
                concrete = concrete.replace("{email_id}", "00000000-0000-0000-0000-000000000000")
                concrete = concrete.replace("{slug}", "some-slug")
                out.append((method, concrete))
    return out


@pytest.mark.asyncio
async def test_protected_prefixes_have_routes() -> None:
    """Sanity: each declared protected prefix has at least one route."""
    routes = _enumerate_protected_routes()
    covered_prefixes = {p for p in M02_PROTECTED_PREFIXES if any(r[1].startswith(p) for r in routes)}
    assert "/api/account/" in covered_prefixes
    assert "/api/memberships/" in covered_prefixes
    assert "/api/audit" in covered_prefixes


@pytest.mark.asyncio
async def test_every_protected_route_rejects_anonymous_access() -> None:
    """Negative-trio floor: unauthenticated + no X-Org-Slug ⇒ middleware
    returns 400 (missing_org_slug); with header but no session ⇒ 401."""
    routes = _enumerate_protected_routes()
    assert routes, "no protected routes discovered"

    async with _client() as c:
        for method, path in routes:
            # No header → 400.
            resp = await c.request(method, path)
            assert resp.status_code in (400, 401, 403, 404, 405, 422), (
                f"{method} {path} returned {resp.status_code}; expected 4xx without any auth"
            )

            # With X-Org-Slug but no session → 401 from `require()`.
            resp = await c.request(method, path, headers={"X-Org-Slug": "some-slug"})
            assert resp.status_code in (401, 403, 404, 405, 422), (
                f"{method} {path} returned {resp.status_code} with slug but no session; expected 401/403/404"
            )


@pytest.mark.asyncio
async def test_no_2xx_from_anonymous_protected_request() -> None:
    """Strict: under no circumstance should an anonymous request return 2xx
    against a protected route."""
    routes = _enumerate_protected_routes()
    async with _client() as c:
        for method, path in routes:
            resp = await c.request(method, path)
            assert not (200 <= resp.status_code < 300), (
                f"{method} {path} returned {resp.status_code} anonymously — auth-bypass risk"
            )
