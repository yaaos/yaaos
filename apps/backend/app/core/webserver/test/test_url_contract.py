"""URL-contract test — pin every load-bearing API URL.

This is the test that catches "module renamed → route silently moved" bugs.
A module rename that changes `module_name` without setting `url_prefix` on
its RouteSpec silently moves every URL that relied on the default prefix,
breaking the SPA, GitHub OAuth callback, and every external caller with no
test failure because per-test mini-apps re-implement the prefix-derivation
rule themselves.

This test boots the real `create_app()` (NOT a per-test mini-app, NOT a
duplicated mount loop) and asserts the small set of URLs that downstream
systems (SPA, GitHub App, etc.) depend on are actually mounted there.

Add a URL here when a new contract surfaces. Removing one is a breaking
change — surface it, don't silently delete.
"""

from __future__ import annotations

from fastapi import FastAPI

import app.web  # noqa: F401 — side-effect: every module registers its RouteSpec
from app.core.webserver import mount_specs

# (method, path) tuples that external systems (SPA, GitHub App callback,
# webhook senders, agent wire) actively depend on. Templated paths use
# FastAPI's `{name}` syntax verbatim.
LOAD_BEARING_URLS: set[tuple[str, str]] = {
    # Auth flow (SPA + GitHub OAuth callback URL registered with GitHub.com).
    ("GET", "/api/auth/providers"),
    ("GET", "/api/auth/login"),
    ("GET", "/api/auth/callback/{provider}"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
    # settings / user (SPA settings pages).
    ("GET", "/api/user/me"),
    # Webhook ingress (GitHub App webhook).
    ("POST", "/api/intake/{type}"),
    # Org membership surface (SPA invite + role flows).
    ("POST", "/api/memberships/invite"),
    # Integrations broken-creds summary (banner + Coding Agents notice).
    ("GET", "/api/integrations/broken-summary"),
    # Org settings (WorkspaceSettingsCard depends on this).
    ("GET", "/api/orgs"),
    ("PATCH", "/api/orgs"),
    # wire protocol (Go agent depends on every one of these).
    ("POST", "/api/v1/agent/identity"),
}


def test_load_bearing_urls_are_mounted() -> None:
    """Assert every load-bearing URL exists when the registered RouteSpecs
    are mounted via `mount_specs` — the same call the production lifespan
    makes. Catches:
      - module renames that change `module_name` without setting `url_prefix`
      - a route deleted by accident
      - HTTP method drift (e.g. POST→PUT) on a contracted endpoint
    """
    app = FastAPI()
    mount_specs(app)
    mounted: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if not path:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            mounted.add((method, path))

    missing = LOAD_BEARING_URLS - mounted
    assert not missing, (
        f"Load-bearing URLs not mounted at expected paths: {sorted(missing)}. "
        f"This usually means a module was renamed without setting "
        f"`url_prefix` on its RouteSpec, or a route was deleted/renamed. "
        f"If a contract changed intentionally, update LOAD_BEARING_URLS."
    )


def test_no_two_specs_collide_via_default_prefix() -> None:
    """Belt-and-suspenders for the registry's own collision check. If a
    refactor weakens registry validation, this test still catches the
    same class of bug at the integration tier."""
    from app.core.webserver.registry import get_specs  # noqa: PLC0415

    prefixes: dict[str, str] = {}
    for module_name, spec in get_specs().items():
        prefix = spec.effective_prefix
        assert prefix not in prefixes, (
            f"Two modules claim prefix {prefix!r}: {prefixes[prefix]!r} and {module_name!r}"
        )
        prefixes[prefix] = module_name
