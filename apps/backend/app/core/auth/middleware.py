"""Default-deny middleware for `/api/*`.

Behavior:

  1. Reset identity contextvars at every request.
  2. Classify the path via `classify_route(path, method)`:
     - `PUBLIC`      → set `route_security_resolved = "public"`, pass through.
     - `USER_SCOPED` → set `route_security_resolved = "user_scoped"`, pass
       through. Session enforcement is the route dep's job (no header
       requirement here).
     - `ORG_SCOPED`  → if the request lacks `X-Yaaos-Org-Slug`, return 400; if it
       mutates without a valid CSRF token, return 403. Otherwise call the
       route; `require(action)` sets `route_security_resolved = "org_scoped"`
       once it resolves the membership.
  3. **Default-deny post-response guard (any `/api/*`):** if the route
     returned 2xx but no security was declared, swap the response for
     500. Non-2xx pass through so dep-raised 401/403/404 aren't masked.

Implemented as pure ASGI middleware (not Starlette `BaseHTTPMiddleware`) so
contextvars set inside the route handler propagate back here when we check
the post-response guard. `BaseHTTPMiddleware` runs the downstream in a
separate task and the contextvar mutations don't reach the dispatch task.
"""

from __future__ import annotations

import json

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth.context import (
    actor_id_var,
    actor_kind_var,
    org_id_var,
    route_security_resolved,
    unbind_request_structlog_vars,
    user_id_var,
)
from app.core.auth.types import RouteSecurity, classify_route, org_slug_in_query_allowed

log = structlog.get_logger("auth.middleware")


def _csrf_ok(request: Request) -> bool:
    """Double-submit check. Both the cookie and the header must be present
    and equal. Empty values are not acceptable.

    A request without a session cookie passes — there's nothing to CSRF; the
    request is anonymous and the route's session check will reject it on
    its own merits."""
    if not request.cookies.get("yaaos_session"):
        return True
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

        category = classify_route(path, request.method)

        if category is RouteSecurity.PUBLIC:
            route_security_resolved.set("public")
            await self.app(scope, receive, send)
            return

        if category is RouteSecurity.USER_SCOPED:
            # No X-Yaaos-Org-Slug requirement; the route dep enforces session.
            route_security_resolved.set("user_scoped")
            # CSRF still applies — session-cookie mutations are vulnerable
            # regardless of whether an org is in scope.
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_ok(request):
                start, body = _json_response(403, {"error": "csrf_mismatch"})
                await send(start)
                await send(body)
                return
            await self.app(scope, receive, send)
            return

        if category is RouteSecurity.ORG_SCOPED:
            # Org slug required — normally via the `X-Yaaos-Org-Slug` header. SSE
            # stream routes also accept it in the `org` query param because the
            # browser `EventSource` API cannot set headers (see
            # `org_slug_in_query_allowed`). The slug runs through the same
            # membership check either way.
            has_org = bool(request.headers.get("X-Yaaos-Org-Slug")) or (
                org_slug_in_query_allowed(path) and bool(request.query_params.get("org"))
            )
            if not has_org:
                start, body = _json_response(400, {"error": "missing_org_slug"})
                await send(start)
                await send(body)
                return

            # Double-submit CSRF on mutating org-scoped requests. The session
            # cookie carries the opaque token; the SPA echoes the per-session
            # csrf token in `X-CSRF-Token`. If the session row's csrf_token
            # doesn't match the header, reject.
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_ok(request):
                start, body = _json_response(403, {"error": "csrf_mismatch"})
                await send(start)
                await send(body)
                return

        # Fall-through (`category is None`, legacy unclassified) and
        # `ORG_SCOPED` both route through the post-response guard below:
        # the route dep is expected to set `route_security_resolved`; if a
        # 2xx escapes without one, the guard substitutes a 500.

        # Wrap `send` so we can intercept the response status. If an
        # ORG_SCOPED route returns 2xx without declaring security via its
        # `require(...)` dep, replace the response with a 500.
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
                # Default-deny: every /api/* route must consume either
                # `Depends(require(...))` or `Depends(public_route)`. If
                # the response is 2xx and no security was declared, swap
                # in a 500. (Non-2xx pass through so dep-raised
                # 401/403/404 aren't masked.)
                if 200 <= sent_status["status"] < 300 and route_security_resolved.get() is None:
                    log.error("auth.route_missing_security_declaration", path=path)
                    err_start, err_body = _json_response(500, {"error": "route_missing_security_declaration"})
                    await send(err_start)
                    await send(err_body)
                    return
                await send(start)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            # structlog contextvars set during the request need explicit
            # cleanup — they live in the global contextvars map, not the
            # request scope.
            unbind_request_structlog_vars()

        # Dims are stamped on every span (including this request-root span) by
        # YaaosDimensionsSpanProcessor.on_start — no manual set_attribute needed.
