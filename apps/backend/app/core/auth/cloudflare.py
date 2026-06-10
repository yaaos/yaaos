"""Cloudflare-only ingress gate.

Rejects any request that does not carry the correct Cloudflare shared-secret
header with HTTP 403. This ensures only traffic routed through the Cloudflare
proxy (which injects the header via a Transform Rule) can reach the backend.
Direct hits to the Fly .fly.dev hostname or Fly IP addresses are rejected.

Exempt path: `/api/health` — Fly's internal machine checker bypasses Cloudflare
and must still reach the health endpoint.

No-op: when `settings.yaaos_cloudflare_ingress_secret` is empty (dev/test/e2e),
the middleware passes all requests through so the local stack is unaffected.

Header: `X-Yaaos-cf-Ingress` — application-defined custom header (the `cf`
segment marks it as Cloudflare-injected; matches the `X-Yaaos-*` / `X-Yaaos-Org-Slug`
convention used elsewhere). The Cloudflare Transform Rule injects this header
with the same value set in the `YAAOS_CLOUDFLARE_INGRESS_SECRET` Fly secret.
The `CF-*` prefix is reserved by Cloudflare for its own managed headers and
rejected by Transform Rules — hence `X-Yaaos-cf-*`, not `CF-*`.

Implemented as pure ASGI (not Starlette `BaseHTTPMiddleware`) to match the
shape of `AuthMiddleware` — `BaseHTTPMiddleware` buffers responses and breaks SSE.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

#: The request header Cloudflare injects via Transform Rule.
#: Must match the header name configured in the Cloudflare dashboard.
CLOUDFLARE_INGRESS_HEADER = "X-Yaaos-cf-Ingress"

#: Path exempt from the ingress check — Fly's internal machine checker
#: bypasses Cloudflare and calls this directly.
_HEALTH_PATH = "/api/health"


def _json_403() -> tuple[dict, dict]:
    payload = json.dumps({"error": "forbidden"}).encode()
    return (
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        },
        {"type": "http.response.body", "body": payload, "more_body": False},
    )


class CloudflareIngressMiddleware:
    """Pure-ASGI Cloudflare ingress gate.

    Registration: must be the second-to-last `app.add_middleware(...)` call in
    `_install_middleware` so it runs as the outermost *security gate*. The
    `CSPMiddleware` is registered after it (and therefore runs strictly
    outermost) so the CSP header lands on every response, including this
    middleware's 403s.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from app.core.config import get_settings  # noqa: PLC0415

        secret = get_settings().yaaos_cloudflare_ingress_secret.get_secret_value()

        # No-op when the secret is empty (dev/test/e2e).
        if not secret:
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # Exempt: Fly's internal health checker bypasses Cloudflare.
        if path == _HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        # Extract the shared-secret header from the ASGI scope.
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        header_value: str | None = None
        header_name_bytes = CLOUDFLARE_INGRESS_HEADER.lower().encode()
        for name, value in headers:
            if name.lower() == header_name_bytes:
                header_value = value.decode("latin-1")
                break

        # Constant-time comparison to prevent timing attacks.
        import hmac  # noqa: PLC0415

        if header_value is None or not hmac.compare_digest(header_value, secret):
            start, body = _json_403()
            await send(start)
            await send(body)
            return

        await self.app(scope, receive, send)
