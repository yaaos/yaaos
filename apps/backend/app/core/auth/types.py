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

    # M04 — hosted-MCP integrations (Linear, Notion, ...). Owner+Admin only.
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_WRITE = "integrations.write"


# Public-allowlist prefixes: any path matching one of these bypasses the
# X-Org-Slug requirement AND the post-response security guard.
PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
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
PUBLIC_PATH_EXACT: frozenset[str] = frozenset({"/api/health", "/api/memberships/accept"})


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
)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATH_EXACT:
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
