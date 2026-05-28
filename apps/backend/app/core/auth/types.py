"""Action enum + route-security classification for `core/auth`.

Every `/api/*` route falls into one of three security categories:

* `RouteSecurity.PUBLIC` — no session, no org context. Login page, health
  check, OAuth callbacks, bearer-authed bridges.
* `RouteSecurity.USER_SCOPED` — session required, but no org. Account
  profile, notifications, "which orgs am I in?".
* `RouteSecurity.ORG_SCOPED` — session **and** a valid `X-Org-Slug` header
  resolving to a membership with sufficient role. Everything that operates
  on a single org's data.

`classify_route(path, method)` performs the lookup. Middleware uses the
result to decide whether to enforce the `X-Org-Slug` header (only
`ORG_SCOPED` requires it). The route's `Depends(...)` chain handles
session lookup + role checks.

Unclassified `/api/*` paths fall through as `PUBLIC` — the routers
predate this taxonomy and aren't backfilled yet. Adding a route under a
new prefix without classifying it is a bug; the post-response guard in
`middleware.py` shouts when a 2xx escapes without `route_security_resolved`
being set.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

# Global default idle-session timeout. A session that hasn't been touched in
# this long is treated as expired by the `require(...)` dep, regardless of its
# absolute `expires_at`. Orgs can override per-org via
# `orgs.session_timeout_override` (nullable minutes) — see
# `domain/orgs.session_timeout`.
SESSION_IDLE_TIMEOUT = timedelta(hours=12)


class Action(StrEnum):
    """The single grep-able action catalogue. Each entry maps to a minimum
    `Role` at the call site of `require(action)`. Adding an action is a
    code change, not config.
    """

    # User-scoped reads. Every member can hit these (the role check is
    # vacuous because the route is USER_SCOPED, but the action is kept for
    # audit-log shape consistency).
    IDENTITY_READ_SELF = "identity.read_self"
    ORG_READ = "org.read"
    MEMBERS_READ = "members.read"
    AUDIT_READ = "audit.read"

    # Org-scoped mutating endpoints — Builder/Admin/Owner depending on action.
    USER_UPDATE_SELF = "user.update_self"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_REMOVE = "members.remove"
    MEMBERS_CHANGE_ROLE = "members.change_role"
    SSO_CONFIGURE = "sso.configure"
    GITHUB_APP_LINK = "github.app_link"
    REVIEW_TRIGGER = "review.trigger"

    # Settings — VCS / coding-agents / BYOK / top-level org. Owner+Admin only.
    VCS_READ = "vcs.read"
    VCS_WRITE = "vcs.write"
    CODING_AGENT_READ = "coding_agent.read"
    CODING_AGENT_WRITE = "coding_agent.write"
    BYOK_READ = "byok.read"
    BYOK_WRITE = "byok.write"
    ORG_SETTINGS_WRITE = "org_settings.write"
    # Owner/Admin can read the workspace-agent connection status.
    ORG_SETTINGS_READ = "org_settings.read"

    # Hosted-MCP integrations (Linear, Notion, ...). Owner+Admin only.
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_WRITE = "integrations.write"

    # Org-scoped tickets / lessons / reviewer. Builder reads + mutates;
    # Admin/Owner inherit via Role.covers().
    TICKETS_READ = "tickets.read"
    LESSONS_READ = "lessons.read"
    LESSONS_WRITE = "lessons.write"
    REVIEWER_READ = "reviewer.read"
    REVIEWER_WRITE = "reviewer.write"


class RouteSecurity(StrEnum):
    """The three route categories. See module docstring."""

    PUBLIC = "public"
    USER_SCOPED = "user_scoped"
    ORG_SCOPED = "org_scoped"


# ---------------------------------------------------------------------------
# Category 1 — PUBLIC: no session, no org context.
# ---------------------------------------------------------------------------

PUBLIC_PREFIXES: tuple[str, ...] = (
    # `/api/sso/{slug}/...` carries the org slug in the path, not the
    # `X-Org-Slug` header. Handlers resolve the slug themselves.
    "/api/sso/",
    # The MCP proxy authenticates via the per-review bearer token, not the
    # session cookie. The yaaos coding-agent CLI doesn't carry `X-Org-Slug`;
    # the path encodes `review_id` and the proxy resolves `org_id` from the
    # review row.
    "/api/mcp/",
    # WorkspaceAgent wire protocol — bearer-authed, not session-authed.
    # `/identity/exchange` is explicitly public (no bearer yet); the other
    # five endpoints use `_bearer_dep` for their own auth check. The
    # session middleware must not gate on `X-Org-Slug` or the CSRF token
    # for these routes, and the post-response guard must not fire.
    "/api/v1/",
)

PUBLIC_EXACT: frozenset[str] = frozenset(
    {
        "/api/health",
        # Acceptance must work for users who have a session but no membership
        # yet — the signed invitation token is the authorization.
        "/api/memberships/accept",
        # `/api/auth/*` login surface — pre-session by definition.
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/logout-all",
        "/api/auth/providers",
        "/api/auth/sso/discover",
        # TOTP flow is used both during login (no session yet) and after
        # login from the user security page; handlers gate by their own state.
        "/api/auth/totp/enroll",
        "/api/auth/totp/challenge",
        "/api/auth/totp/verify",
    }
)

# `/api/auth/callback/{provider}` — OAuth provider redirects here with no
# session cookie. Prefix-matched because `{provider}` is a path param.
PUBLIC_PREFIX_VARIABLE: tuple[str, ...] = (
    "/api/auth/callback/",
    # OAuth callback URLs under /api/mcp-proxy/{provider}/callback. The
    # upstream provider doesn't know about our X-Org-Slug header; the signed
    # `state` carries the org_id.
    # (Matched via the dedicated suffix check below.)
)


# ---------------------------------------------------------------------------
# Category 2 — USER_SCOPED: session required, no org context.
# ---------------------------------------------------------------------------

USER_SCOPED_PREFIXES: tuple[str, ...] = (
    # User profile + email management. Session via `_require_user()`.
    "/api/user/",
    # Cross-org notification stream. Session cookie identifies the recipient;
    # org filters are optional query params.
    "/api/notifications",
)

USER_SCOPED_EXACT: frozenset[str] = frozenset(
    {
        # Current-user lookup. SPA hits this before the org is known; on
        # success the SPA picks an org and sets X-Org-Slug on later calls.
        "/api/auth/me",
        # User-scoped (cross-org) listing of the user's memberships.
        "/api/orgs/mine",
    }
)

# (method, path) tuples that override the prefix-based classification for a
# single verb. Used for `POST /api/orgs` (org-create runs before any org is
# selected) while `GET /api/orgs` (org-settings read) stays ORG_SCOPED.
USER_SCOPED_METHOD_EXACT: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/orgs"),
    }
)


# ---------------------------------------------------------------------------
# Category 3 — ORG_SCOPED: session + X-Org-Slug + role check.
# ---------------------------------------------------------------------------

ORG_SCOPED_PREFIXES: tuple[str, ...] = (
    "/api/memberships/",
    "/api/audit",  # exact + prefix both
    "/api/plugins/",
    "/api/vcs",  # exact + prefix
    "/api/coding-agents",
    "/api/orgs",  # exact + prefix; POST is exempted via USER_SCOPED_METHOD_EXACT
    "/api/api-keys",
    "/api/mcp-proxy",
    # Workspace connection status + activity SSE stream.
    "/api/workspaces",
    # Org-scoped tickets / lessons / reviewer.
    "/api/tickets",
    "/api/lessons",
    "/api/reviewer",
    # SSE routes mounted at core/sse/web.py — org-scoped per-workflow streams.
    "/api/sse",
)


# ---------------------------------------------------------------------------
# Classifier.
# ---------------------------------------------------------------------------


def classify_route(path: str, method: str | None = None) -> RouteSecurity | None:
    """Return the security category for `(method, path)`, or `None` if the
    path isn't explicitly classified.

    Precedence: method-specific exact > exact > prefix. Within those tiers,
    PUBLIC and USER_SCOPED are checked before ORG_SCOPED so the cross-org
    overlays for `/api/orgs/mine` and `POST /api/orgs` win over the
    `/api/orgs` prefix.

    Returning `None` (legacy unclassified) tells the middleware to skip its
    header / CSRF enforcement and rely on the route dep + post-response
    guard. Adding a new route under a brand-new prefix is therefore safe in
    isolation — until you classify it, the guard returns 500 if you forgot
    a security dep.
    """
    # Method-specific exact overrides.
    if method is not None and (method, path) in USER_SCOPED_METHOD_EXACT:
        return RouteSecurity.USER_SCOPED

    # Exact matches.
    if path in PUBLIC_EXACT:
        return RouteSecurity.PUBLIC
    if path in USER_SCOPED_EXACT:
        return RouteSecurity.USER_SCOPED

    # Prefix matches.
    if any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return RouteSecurity.PUBLIC
    if any(path.startswith(p) for p in PUBLIC_PREFIX_VARIABLE):
        return RouteSecurity.PUBLIC
    # `/api/mcp-proxy/{provider}/callback` — OAuth redirect target; bypass
    # X-Org-Slug. Only the `/callback` suffix is public; `/connect`,
    # `/validate`, etc. stay ORG_SCOPED.
    if path.startswith("/api/mcp-proxy/") and path.endswith("/callback"):
        return RouteSecurity.PUBLIC
    if any(path.startswith(p) for p in USER_SCOPED_PREFIXES):
        return RouteSecurity.USER_SCOPED
    if any(path == p.rstrip("/") or path.startswith(p) for p in ORG_SCOPED_PREFIXES):
        return RouteSecurity.ORG_SCOPED

    return None


def is_org_scoped_path(path: str, method: str | None = None) -> bool:
    """Shortcut: returns True iff `(method, path)` classifies as ORG_SCOPED."""
    return classify_route(path, method) is RouteSecurity.ORG_SCOPED
