"""Action enum + low-level types for `core/auth`."""

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

    # M02 read endpoints — every member can hit these.
    IDENTITY_READ_SELF = "identity.read_self"
    ORG_READ = "org.read"
    MEMBERS_READ = "members.read"
    AUDIT_READ = "audit.read"

    # M02 mutating endpoints.
    ACCOUNT_UPDATE_SELF = "account.update_self"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_REMOVE = "members.remove"
    MEMBERS_CHANGE_ROLE = "members.change_role"
    SSO_CONFIGURE = "sso.configure"
    GITHUB_APP_LINK = "github.app_link"
    REVIEW_TRIGGER = "review.trigger"

    # M03 settings — VCS / coding-agents / BYOK / top-level org. Owner+Admin only.
    VCS_READ = "vcs.read"
    VCS_WRITE = "vcs.write"
    CODING_AGENT_READ = "coding_agent.read"
    CODING_AGENT_WRITE = "coding_agent.write"
    BYOK_READ = "byok.read"
    BYOK_WRITE = "byok.write"
    ORG_SETTINGS_WRITE = "org_settings.write"
    # M05 Phase 7 — Owner/Admin can read the workspace-agent connection status.
    ORG_SETTINGS_READ = "org_settings.read"

    # M04 — hosted-MCP integrations (Linear, Notion, ...). Owner+Admin only.
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_WRITE = "integrations.write"

    # M06 — org-scope the M01-era routers (tickets, lessons, reviewer). Every
    # Builder reads + mutates; Admin/Owner inherit via Role.covers().
    TICKETS_READ = "tickets.read"
    LESSONS_READ = "lessons.read"
    LESSONS_WRITE = "lessons.write"
    REVIEWER_READ = "reviewer.read"
    REVIEWER_WRITE = "reviewer.write"


# Public-allowlist prefixes: any path matching one of these bypasses the
# X-Org-Slug requirement AND the post-response security guard.
PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
    # M06 — notifications are user-scoped (cross-org). The session cookie
    # identifies the recipient; org filters are optional query params.
    "/api/notifications",
    # `/api/sso/{slug}/...` carries the org slug in the path, not the
    # `X-Org-Slug` header. The handlers resolve the slug themselves.
    # `/api/sso/config` (Owner-only) goes through the standard auth chain
    # via the path-prefix override below.
    "/api/sso/",
    # M04 — the MCP proxy authenticates via the per-review bearer token,
    # not the session cookie. The yaaos coding-agent CLI doesn't carry
    # `X-Org-Slug`; the path encodes `review_id` and the proxy resolves
    # `org_id` from the review row.
    "/api/mcp/",
)
# `/api/memberships/accept` lives on the public allowlist because acceptance
# must work for users who have a session but no membership yet — the signed
# invitation token is the authorization, not an org membership.
PUBLIC_PATH_EXACT: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/memberships/accept",
        # M06 — user-scoped (cross-org) listing. Session cookie identifies the
        # user; no `X-Org-Slug` because the endpoint enumerates the user's orgs.
        "/api/orgs/mine",
    }
)


# Paths the auth middleware enforces on. M02 routes opt in by adding their
# prefix here; legacy /api/* routes are not yet covered so existing endpoints
# keep working through the transition. Phase 14 expands this set as the
# backfill completes.
M02_PROTECTED_PREFIXES: tuple[str, ...] = (
    "/api/account/",
    "/api/memberships/",
    "/api/audit",  # exact + prefix both — endpoint is /api/audit and /api/audit/...
    "/api/plugins/",
    "/api/vcs",  # exact + prefix
    "/api/coding-agents",  # exact + prefix
    "/api/orgs",  # exact + prefix
    "/api/byok",  # exact + prefix
    "/api/integrations",  # exact + prefix
    # M05 — workspace connection status + activity SSE stream.
    "/api/workspaces",  # exact + prefix
    # M06 — org-scope the three M01-era routers.
    "/api/tickets",  # exact + prefix
    "/api/lessons",  # exact + prefix
    "/api/reviewer",  # exact + prefix
)


# M06 — (method, path) tuples that bypass X-Org-Slug for a single verb on a
# path that otherwise requires it. Used for `POST /api/orgs` (org-create) so
# the picker page can hit it before any org is selected, while `GET /api/orgs`
# (org-settings read) keeps requiring X-Org-Slug.
PUBLIC_METHOD_EXACT: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/orgs"),
    }
)


def is_public_path(path: str, method: str | None = None) -> bool:
    if path in PUBLIC_PATH_EXACT:
        return True
    if method is not None and (method, path) in PUBLIC_METHOD_EXACT:
        return True
    if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return True
    # M04 — OAuth callback URLs under /api/integrations/{provider}/callback.
    # The upstream OAuth provider doesn't know about our X-Org-Slug header;
    # the signed `state` carries the org_id. Only the exact `/callback`
    # suffix is public — `/connect`, `/validate`, etc. stay protected.
    if path.startswith("/api/integrations/") and path.endswith("/callback"):
        return True
    return False


def is_m02_protected_path(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in M02_PROTECTED_PREFIXES)
