"""Default-deny middleware for `/api/*`.

Behavior (only on M02-protected paths — see `types.M02_PROTECTED_PREFIXES`):

  1. Reset identity contextvars.
  2. If path is in the public allowlist (`/api/auth/*`, `/api/health`),
     pass through without further enforcement.
  3. Otherwise require `X-Org-Slug` header — else 400.
  4. Call the route. Route deps (`require(action)` / `public_route`) populate
     `route_security_resolved`.
  5. Post-response: if `route_security_resolved` is None on an M02-protected
     path and the response is a 2xx, log + 500 (a route forgot its security
     declaration). Non-2xx responses pass through (the dep raised an
     intended HTTPException — the post-response guard would otherwise mask
     legitimate 401/403/404s with a misleading 500).

Implemented as pure ASGI middleware (not Starlette `BaseHTTPMiddleware`) so
contextvars set inside the route handler propagate back here when we check
the post-response guard. `BaseHTTPMiddleware` runs the downstream in a
separate task and the contextvar mutations don't reach the dispatch task.
"""

from __future__ import annotations

import json

import structlog
from opentelemetry import trace
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth.context import (
    actor_id_var,
    actor_kind_var,
    org_id_var,
    route_security_resolved,
    user_id_var,
)
from app.core.auth.types import is_m02_protected_path, is_public_path

log = structlog.get_logger("auth.middleware")


def _csrf_ok(request: Request) -> bool:
    """Double-submit check. Both the cookie and the header must be present
    and equal. Empty values are not acceptable."""
    cookie = request.cookies.get("yaaos_csrf")
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header:
        return False
    return secrets_compare(cookie, header)


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time string equality."""
    import hmac  # noqa: PLC0415

    return hmac.compare_digest(a, b)


def _json_response(status: int, body: dict[str, object]) -> tuple[Message, Message]:
    payload = json.dumps(body).encode()
    return (
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        },
        {"type": "http.response.body", "body": payload, "more_body": False},
    )


class AuthMiddleware:
    """See module docstring."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path

        # Reset contextvars at the start of every request.
        org_id_var.set(None)
        user_id_var.set(None)
        actor_kind_var.set(None)
        actor_id_var.set(None)
        route_security_resolved.set(None)

        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        if is_public_path(path):
            route_security_resolved.set("public")
            await self.app(scope, receive, send)
            return

        protected = is_m02_protected_path(path)
        if protected and not request.headers.get("X-Org-Slug"):
            start, body = _json_response(400, {"error": "missing_org_slug"})
            await send(start)
            await send(body)
            return

        # Double-submit CSRF on mutating requests. The session cookie carries
        # the opaque token; the SPA echoes the per-session csrf token in
        # `X-CSRF-Token`. If the session row's csrf_token doesn't match the
        # header, reject. Skipped for safe methods + non-protected paths +
        # the public allowlist (handled above).
        if protected and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_ok(request):
            start, body = _json_response(403, {"error": "csrf_mismatch"})
            await send(start)
            await send(body)
            return

        # Wrap `send` so we can intercept the response status. If a route
        # under an M02-protected prefix returns 2xx without declaring
        # security, we replace the response with a 500 instead.
        sent_status: dict[str, int] = {}
        intercepted_start: list[Message] = []

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                sent_status["status"] = message["status"]
                # Defer sending the start message until after we've seen
                # the body — we may want to substitute a 500 instead.
                intercepted_start.append(message)
                return
            # body or trailers
            if message["type"] == "http.response.body" and intercepted_start:
                start = intercepted_start.pop(0)
                if protected and 200 <= sent_status["status"] < 300 and route_security_resolved.get() is None:
                    log.error("auth.route_missing_security_declaration", path=path)
                    err_start, err_body = _json_response(500, {"error": "route_missing_security_declaration"})
                    await send(err_start)
                    await send(err_body)
                    return
                await send(start)
            await send(message)

        await self.app(scope, receive, _send)

        # Tag the OTel span (best-effort).
        span = trace.get_current_span()
        if span is not None:
            org_id = org_id_var.get()
            user_id = user_id_var.get()
            actor_kind = actor_kind_var.get()
            if org_id is not None:
                span.set_attribute("yaaos.org_id", str(org_id))
            if user_id is not None:
                span.set_attribute("yaaos.user_id", str(user_id))
            if actor_kind is not None:
                span.set_attribute("yaaos.actor_kind", actor_kind.value)
