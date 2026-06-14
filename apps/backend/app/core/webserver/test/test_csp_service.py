"""Service tests for the Content-Security-Policy middleware.

The middleware injects either `Content-Security-Policy` or
`Content-Security-Policy-Report-Only` on every response. The policy directive
list is a single source of truth in `core/webserver/csp.CSP_POLICY`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.service
def test_csp_header_present_in_report_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default mode is report-only — every response carries
    `Content-Security-Policy-Report-Only`, never the enforcing header."""
    monkeypatch.setenv("YAAOS_CSP_MODE", "report-only")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver import create_app  # noqa: PLC0415

    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/health")

    assert "Content-Security-Policy-Report-Only" in resp.headers
    assert "Content-Security-Policy" not in resp.headers or resp.headers.get(
        "Content-Security-Policy"
    ) == resp.headers.get("Content-Security-Policy-Report-Only")
    policy = resp.headers["Content-Security-Policy-Report-Only"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "https://ingress.us-west-2.aws.dash0.com" in policy
    assert "https://fonts.gstatic.com" in policy
    assert "frame-ancestors 'none'" in policy

    get_settings.cache_clear()


@pytest.mark.service
def test_csp_header_present_in_enforce_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`YAAOS_CSP_MODE=enforce` flips the header name from report-only to enforcing.
    The policy string is the same; only the header name changes."""
    monkeypatch.setenv("YAAOS_CSP_MODE", "enforce")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver import create_app  # noqa: PLC0415

    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/health")

    assert "Content-Security-Policy" in resp.headers
    assert "Content-Security-Policy-Report-Only" not in resp.headers
    policy = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy
    assert "https://ingress.us-west-2.aws.dash0.com" in policy

    get_settings.cache_clear()


@pytest.mark.service
def test_csp_header_set_on_cloudflare_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critical ordering invariant: CSPMiddleware runs OUTSIDE CloudflareIngressMiddleware,
    so a 403 from the Cloudflare gate still carries the CSP header on its way out.

    Set the ingress secret to a real value, send a request without the matching
    `X-Yaaos-cf-Ingress` header, expect 403 + CSP header present.
    """
    monkeypatch.setenv("YAAOS_CSP_MODE", "report-only")
    monkeypatch.setenv("YAAOS_CLOUDFLARE_INGRESS_SECRET", "secret-for-this-test")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver import create_app  # noqa: PLC0415

    app = create_app()
    client = TestClient(app)
    # Hit a non-health path (health is exempt from the Cloudflare gate) without
    # the matching header — the gate must 403.
    resp = client.get("/api/auth/me")

    assert resp.status_code == 403, f"expected 403 from Cloudflare gate, got {resp.status_code}"
    assert "Content-Security-Policy-Report-Only" in resp.headers, (
        "CSP header must land on Cloudflare's 403 — CSPMiddleware must be outermost"
    )

    get_settings.cache_clear()


@pytest.mark.service
def test_csp_header_set_on_spa_index_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """CSP must apply to the SPA index.html catch-all too, not just /api/* —
    the SPA HTML is what loads the scripts/styles the policy governs."""
    monkeypatch.setenv("YAAOS_CSP_MODE", "report-only")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver import create_app  # noqa: PLC0415

    app = create_app()
    client = TestClient(app)
    # The SPA catch-all returns 200 (with index.html) when apps/web/dist exists,
    # or 404 when it doesn't. Either response must carry the CSP header — the
    # middleware runs outermost.
    resp = client.get("/some-spa-route")
    assert "Content-Security-Policy-Report-Only" in resp.headers, (
        f"CSP header missing on SPA-path response {resp.status_code}"
    )

    get_settings.cache_clear()
