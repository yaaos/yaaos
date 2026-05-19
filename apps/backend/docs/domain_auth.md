# domain/auth

> FastAPI dependency factories that wire the [`core/auth`](core_auth.md) middleware into identity + orgs lookups.

## Purpose

`core/auth` ships the pure middleware, contextvars, and the `Action` enum. The actual session-cookie → user lookup and slug → org → membership → role check happen here because they need both `domain/identity` and `domain/orgs` — dependencies that `core/auth` can't take (core can't depend on domain).

## Public interface

Exported from `app/domain/auth/__init__.py`:

- `require(action)` — dependency factory. Resolves `X-Org-Slug` → org → membership → role check. Sets the identity contextvars + `route_security_resolved = "membership"`. Returns the `Membership` so handlers that want it can `Depends(require(...))` directly.
- `public_route` — dependency for routes that intentionally have no auth requirement. Sets `route_security_resolved = "public"`. Using this where a role check should live is the bug the post-response guard catches.
- `current_actor()` — reads `user_id_var` and returns an `Actor.user(user_id=…)` for audit-log writes. Raises if no session resolved.
- `required_role_for(action)` — lookup the minimum role for an action.

## Module architecture

### Session resolution

`_current_session_user_id` reads the `yaaos_session` cookie, sha256-hashes it, looks up the row, validates expiry, and sets `user_id_var`. None is returned for missing/expired/unknown sessions; the caller (`require`) raises 401.

### Error shape

- No session → 401 `unauthenticated`.
- No `X-Org-Slug` → middleware already 400'd; this dep won't reach the check.
- Org doesn't exist OR caller has no membership in it → 404 `org_not_found`. Mask existence — never leak "the org is real but you can't see it."
- Role insufficient → 403 `insufficient_role`.

### `_REQUIRED_ROLE` registry

Single source of truth mapping `Action → Role`. Per-endpoint overrides are explicit: write `Depends(require(Action.X))` whose row in this map is the role you want enforced.

| Action | Required role |
|---|---|
| `IDENTITY_READ_SELF` | Member |
| `ORG_READ` | Member |
| `MEMBERS_READ` | Member |
| `AUDIT_READ` | Admin |
| `ACCOUNT_UPDATE_SELF` | Member |
| `MEMBERS_INVITE` | Admin |
| `MEMBERS_REMOVE` | Admin |
| `MEMBERS_CHANGE_ROLE` | Admin |
| `SSO_CONFIGURE` | Owner |
| `GITHUB_APP_LINK` | Owner |
| `REVIEW_TRIGGER` | Member |

A coverage test asserts every `Action` member has a row here.

## Data owned

None — reads `sessions`, `orgs`, `memberships` via the identity/orgs repositories.

## How it's tested

`test/test_middleware.py` covers the full chain — middleware header check, dep resolution, role check, contextvar propagation. See [`core/auth`](core_auth.md) for the test inventory.
