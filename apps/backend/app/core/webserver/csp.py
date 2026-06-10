"""Content-Security-Policy middleware.

Injects either `Content-Security-Policy` or
`Content-Security-Policy-Report-Only` on every HTTP response, controlled by
`Settings.yaaos_csp_mode`. The policy itself is a single string constant —
no per-route customization, no extra-host configuration. Adding a host means
changing this file and shipping.

Why a pure-ASGI middleware (not `BaseHTTPMiddleware`): `BaseHTTPMiddleware`
buffers the response body, which breaks SSE. Wrapping `send` to inject one
header on the `http.response.start` message is loop-overhead-free.

Why outermost: the middleware must set the header on EVERY response — including
Cloudflare ingress 403s, auth 401s, rate-limit 429s. Registered LAST in
`_install_middleware` so it runs outermost (FastAPI reverses registration order).

Directive rationale per directive — verified against actual SPA load behavior:
- `default-src 'self'` — same-origin baseline. Catches anything not explicitly listed.
- `script-src 'self'` — Vite emits `dist/index.html` with one external `<script type="module" src="/assets/...">` only; no inline scripts.
- `style-src 'self' 'unsafe-inline' https://fonts.googleapis.com` — bundled CSS (self) + Google Fonts CSS (external) + Radix/Tailwind inject inline styles at runtime (popovers, animations).
- `font-src 'self' https://fonts.gstatic.com` — Geist font files served from gstatic.
- `img-src 'self' data:` — SPA logos at `/logos/*.svg` only; `data:` covers inline SVGs.
- `connect-src 'self' https://ingress.europe-west4.gcp.dash0.com` — same-origin `/api/*` + SSE + Dash0 OTLP from the browser.
- `frame-ancestors 'none'` — yaaos is a tool, not meant to be embedded. Blocks clickjacking. Only honored via header, not `<meta>` — which is one of the two reasons we use a header.
- `form-action 'self'` — every SPA form is JS-handler (`onSubmit`); no external `action=`.
- `base-uri 'self'` — prevents `<base href>` injection.
- `object-src 'none'` — no `<object>`/`<embed>`/`<applet>`.
- `upgrade-insecure-requests` — http:// resources get auto-upgraded to https://.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

#: Single source of truth for the policy directive list. Joined with `; ` to
#: form the header value. Add a host or a directive HERE; do not template per-route.
_CSP_DIRECTIVES: tuple[str, ...] = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self' https://ingress.europe-west4.gcp.dash0.com",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "upgrade-insecure-requests",
)

CSP_POLICY: str = "; ".join(_CSP_DIRECTIVES)


class CSPMiddleware:
    """Pure-ASGI middleware that injects the CSP header on every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        mode = get_settings().yaaos_csp_mode
        header_name = (
            b"content-security-policy" if mode == "enforce" else b"content-security-policy-report-only"
        )
        header_value = CSP_POLICY.encode("ascii")

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Drop any pre-existing CSP header (defensive — nothing else sets it today).
                headers = [
                    (name, value)
                    for name, value in headers
                    if name.lower()
                    not in (b"content-security-policy", b"content-security-policy-report-only")
                ]
                headers.append((header_name, header_value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_csp)
